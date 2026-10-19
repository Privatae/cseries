/* ================================================================
 * Calendar — your events and your Host's scheduled work.
 *
 * One table holds it all (calendar_events, in the Host's own database):
 *   - the Host's scheduled tasks: event_type 'scheduled_task', fired by the
 *     Host's task runner, each run a row of calendar_event_runs. A recurring
 *     task is ONE row whose time moves on to its next run after each run.
 *   - the Host's log of what happened: an app installed, a PR shipped.
 *   - the person's own events: source 'user', written by + Event here.
 *
 * Reads:
 *   GET  /api/calendar/list               since, until, event_type, status,
 *                                         order (asc = soonest first), limit, offset
 *   GET  /api/calendar/read/{id}
 *   GET  /api/calendar/events/{id}/runs   limit, offset; newest first
 * Writes:
 *   POST /api/calendar                    {event_type, summary, event_at, status, source}
 *   POST /api/calendar/complete/{id}
 *   POST /api/calendar/cancel/{id}        ?reason=
 *
 * EVERYTHING FROM THE TABLE IS TEXT: titles, a task's prompt and a run's
 * output are written by the Host, a model or another app, so nothing here
 * goes through innerHTML.
 * ================================================================ */
(function () {
    'use strict';

    // A list reads PAGE rows at a time. The month reads its six weeks soonest
    // first, MONTH_PAGE rows a read (the route's largest), and draws at most
    // MONTH_PAGES of them: a Host that logs heavily could otherwise put tens of
    // thousands of chips in one grid. Choosing one kind narrows it.
    const PAGE = 50;
    const MONTH_PAGE = 500;
    const MONTH_PAGES = 6;
    const RUNS_PAGE = 20;
    const REFRESH_MS = 60000;

    const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
                    'July', 'August', 'September', 'October', 'November', 'December'];
    const TYPE_LABELS = {
        scheduled_task: "Your Host's task",
        event: 'Event',
        pr_shipped: 'PR shipped',
        app_built: 'App built',
        app_installed: 'App installed',
        app_update_available: 'App update available',
        inference_run: 'Inference run',
        permission_granted: 'Permission granted',
        daily_summary: 'Daily report',
        research_task: 'Research',
        artifact_created: 'Artifact',
    };
    const STATUS_WORD = { scheduled: 'scheduled', running: 'running', completed: 'done',
                          failed: 'failed', cancelled: 'cancelled' };
    const STATUS_TONE = { scheduled: 'warn', running: 'info', completed: 'allow',
                          failed: 'deny', cancelled: 'deny' };
    // A request the Host keeps for the person's next message is stored as a
    // cancelled task with this reason; it reads as held, never as a failure.
    const HELD_FOR_PERSON = 'held_for_person';
    const FILTERS = ['upcoming', 'today', 'all', 'tasks'];
    const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

    const state = {
        cursor: null,               // {y, m}: the month on screen
        filter: 'upcoming',
        day: null,                  // 'YYYY-MM-DD' picked in the grid
        month: { events: [], truncated: false, error: null, loading: true },
        list: { events: [], next: null, error: null, loading: true },
        active: null,               // the event open in full
        runs: { runs: [], next: null, total: 0, loaded: false, error: null },
        rawOpen: false,
        openRuns: new Set(),        // the runs opened on the page (by when they ran), kept across a refresh
        // Each read carries the number it was sent under; an answer a newer
        // read has overtaken is dropped instead of drawn over the newer one.
        monthSeq: 0, listSeq: 0, detailSeq: 0,
        confirm: null,
        timer: null,
    };

    // ── small DOM helpers (text only) ────────────────────────────────────
    const $ = (id) => document.getElementById(id);
    function el(tag, cls, text) {
        const n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text != null) n.textContent = String(text);
        return n;
    }
    function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
    function pill(text, tone) { return el('span', 'cseries-pill' + (tone ? ' ' + tone : ''), text); }
    function button(text, cls) {
        const b = el('button', cls, text);
        b.type = 'button';
        return b;
    }
    function banner(node, text, tone) {
        if (!text) { node.hidden = true; node.textContent = ''; return; }
        node.className = 'status-banner ' + (tone || 'info');
        node.textContent = text;
        node.hidden = false;
    }
    function stamp() { $('cal-updated').textContent = 'read: ' + new Date().toLocaleTimeString(); }
    const isPhone = () => window.matchMedia('(max-width: 768px)').matches;

    // ── the API ──────────────────────────────────────────────────────────
    // Every call is a literal fetch('/api/calendar/...') so the platform's
    // conformance gate can read which routes this app calls.
    async function json(pending) {
        const r = await pending;
        let body = null;
        try { body = await r.json(); } catch (e) { body = null; }
        if (!r.ok) {
            const why = body && (body.detail || body.error);
            const err = new Error(typeof why === 'string' ? why : (why ? JSON.stringify(why) : 'HTTP ' + r.status));
            err.status = r.status;
            throw err;
        }
        return body;
    }
    function qs(params) {
        return Object.keys(params)
            .filter((k) => params[k] !== null && params[k] !== undefined && params[k] !== '')
            .map((k) => encodeURIComponent(k) + '=' + encodeURIComponent(params[k]))
            .join('&');
    }

    // ── days and times (always the person's own clock) ───────────────────
    const pad = (n) => (n < 10 ? '0' : '') + n;
    function dayKey(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
    function parseDayKey(k) {
        const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(k || '');
        if (!m) return null;
        const d = new Date(+m[1], +m[2] - 1, +m[3]);
        return (d.getMonth() === +m[2] - 1 && d.getDate() === +m[3]) ? d : null;
    }
    function startOfDay(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }
    function endOfDay(d) { return new Date(new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1).getTime() - 1); }
    // Six weeks, Sunday first: the month's own days and the ones either side.
    function gridDays(y, m) {
        const lead = new Date(y, m, 1).getDay();
        const out = [];
        for (let i = 0; i < 42; i++) out.push(new Date(y, m, 1 - lead + i));
        return out;
    }
    function when(iso) {
        if (!iso) return null;
        const d = new Date(iso);
        return isNaN(d.getTime()) ? null : d;
    }
    function timeOf(d) { return d ? d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }) : ''; }
    function dayLabel(d) {
        const today = startOfDay(new Date());
        const diff = Math.round((startOfDay(d).getTime() - today.getTime()) / 86400000);
        if (diff === 0) return 'Today';
        if (diff === 1) return 'Tomorrow';
        if (diff === -1) return 'Yesterday';
        const opts = { weekday: 'short', day: 'numeric', month: 'short' };
        if (d.getFullYear() !== today.getFullYear()) opts.year = 'numeric';
        return d.toLocaleDateString(undefined, opts);
    }
    function fullWhen(d) {
        return d.toLocaleString(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
                                             hour: 'numeric', minute: '2-digit' });
    }
    function shortWhen(d) {
        const opts = { weekday: 'short', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' };
        if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
        return d.toLocaleString(undefined, opts);
    }
    function rel(d) {
        if (!d) return '';
        let s = Math.round((d.getTime() - Date.now()) / 1000);
        const future = s > 0;
        s = Math.abs(s);
        if (s < 60) return 'now';
        let out;
        if (s < 3600) out = Math.round(s / 60) + ' min';
        else if (s < 86400) out = Math.round(s / 3600) + ' h';
        else out = Math.round(s / 86400) + ' d';
        return future ? 'in ' + out : out + ' ago';
    }
    // The runner's own reading of a recurrence (entity_clock._recurrence_seconds):
    // hourly, daily, weekly, monthly (30 days) or every:<seconds>; anything
    // else does not repeat.
    function repeats(rule) {
        const r = String(rule || '').trim().toLowerCase();
        const named = { hourly: 'every hour', daily: 'every day', weekly: 'every week',
                        monthly: 'monthly (every 30 days)' };
        if (named[r]) return named[r];
        const m = /^every:(\d+)$/.exec(r);
        const s = m ? parseInt(m[1], 10) : 0;
        if (!s) return '';
        const unit = (n, one, many) => (n === 1 ? 'every ' + one : 'every ' + n + ' ' + many);
        if (s % 86400 === 0) return unit(s / 86400, 'day', 'days');
        if (s % 3600 === 0) return unit(s / 3600, 'hour', 'hours');
        if (s % 60 === 0) return unit(s / 60, 'minute', 'minutes');
        return unit(s, 'second', 'seconds');
    }

    // ── what an event is ─────────────────────────────────────────────────
    const isTask = (e) => e.event_type === 'scheduled_task';
    function kindOf(e) { return isTask(e) ? 'task' : (e.source === 'user' ? 'yours' : 'log'); }
    function typeLabel(t) {
        if (TYPE_LABELS[t]) return TYPE_LABELS[t];
        const s = String(t || '').replace(/_/g, ' ').trim();
        return s ? s.charAt(0).toUpperCase() + s.slice(1) : 'Event';
    }
    function kindLabel(e) {
        if (isTask(e)) return TYPE_LABELS.scheduled_task;
        if (e.source === 'user') return e.event_type === 'event' ? 'Your event' : 'Yours · ' + typeLabel(e.event_type);
        return typeLabel(e.event_type);
    }
    function isHeld(e) { return e.status === 'cancelled' && (e.cancelled_reason === HELD_FOR_PERSON || e.held === true); }
    function statusWord(e) { return isHeld(e) ? 'held' : (STATUS_WORD[e.status] || e.status || ''); }
    function statusTone(e) { return isHeld(e) ? 'warn' : (STATUS_TONE[e.status] || ''); }
    // Mark done and Cancel: only what is waiting or running.
    function isOpen(e) { return e.status === 'scheduled' || e.status === 'running'; }
    function setBy(e) {
        const who = { user: 'You', entity: 'Your Host', platform: 'The platform' };
        return who[e.source] || e.source || '';
    }
    function kindFilter(f) {
        if (f === 'tasks') return 'scheduled_task';
        return f.indexOf('type:') === 0 ? f.slice(5) : '';
    }

    // ── the month ────────────────────────────────────────────────────────
    async function loadMonth() {
        const seq = ++state.monthSeq;
        state.month.loading = true;
        const days = gridDays(state.cursor.y, state.cursor.m);
        const query = {
            since: startOfDay(days[0]).toISOString(),
            until: endOfDay(days[41]).toISOString(),
            event_type: kindFilter(state.filter),
            order: 'asc',
            limit: MONTH_PAGE,
        };
        const events = [];
        let truncated = false;
        let error = null;
        try {
            for (let page = 0, from = null; ; page++) {
                const q = Object.assign({}, query, from || { offset: 0 });
                const d = await json(fetch('/api/calendar/list?' + qs(q)));
                if (seq !== state.monthSeq) return;
                Array.prototype.push.apply(events, unseen(events, d.events || []));
                if (d.next_offset == null) break;
                if (page + 1 >= MONTH_PAGES) { truncated = true; break; }
                from = after(events, 'asc');
            }
        } catch (e) {
            error = e;
        }
        if (seq !== state.monthSeq) return;
        state.month = { events: events, truncated: truncated, error: error, loading: false };
        renderGrid();
        if (state.day) renderList();
        stamp();
    }

    function byDay(events) {
        const map = new Map();
        events.forEach((e) => {
            const d = when(e.event_at);
            if (!d) return;
            const k = dayKey(d);
            if (!map.has(k)) map.set(k, []);
            map.get(k).push(e);
        });
        return map;
    }

    function renderGrid() {
        const y = state.cursor.y;
        const m = state.cursor.m;
        $('cal-month-label').textContent = MONTHS[m] + ' ' + y;
        const grid = $('cal-grid');
        clear(grid);
        const today = dayKey(new Date());
        const days = byDay(state.month.events);
        gridDays(y, m).forEach((d) => {
            const k = dayKey(d);
            const evs = days.get(k) || [];
            const cell = button(null, 'cal-cell');
            cell.dataset.day = k;
            if (d.getMonth() !== m) cell.classList.add('out');
            if (k === today) cell.classList.add('today');
            if (k === state.day) cell.classList.add('picked');
            cell.setAttribute('aria-pressed', k === state.day ? 'true' : 'false');
            cell.setAttribute('aria-label', d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })
                + (evs.length ? ', ' + evs.length + (evs.length === 1 ? ' event' : ' events') : ''));
            cell.appendChild(el('span', 'cal-daynum', d.getDate()));
            // A desktop shows each event's time and title; a phone, a dot each.
            const chips = el('span', 'cal-chips');
            const dots = el('span', 'cal-dots');
            evs.slice(0, 3).forEach((e) => {
                const chip = el('span', 'cal-chip');
                chip.appendChild(el('span', 'cal-dot k-' + kindOf(e)));
                chip.appendChild(el('span', 'cal-chip-time', timeOf(when(e.event_at))));
                chip.appendChild(el('span', 'cal-chip-text', e.summary || '(no title)'));
                chips.appendChild(chip);
                dots.appendChild(el('span', 'cal-dot k-' + kindOf(e)));
            });
            if (evs.length > 3) {
                chips.appendChild(el('span', 'cal-more-n', '+' + (evs.length - 3) + ' more'));
                dots.appendChild(el('span', 'cal-more-n', '+' + (evs.length - 3)));
            }
            cell.appendChild(chips);
            cell.appendChild(dots);
            cell.addEventListener('click', () => pickDay(k));
            grid.appendChild(cell);
        });
        const note = $('cal-grid-note');
        if (state.month.error) {
            note.textContent = 'This month could not be read: ' + state.month.error.message;
            note.hidden = false;
        } else if (state.month.truncated) {
            note.textContent = 'More than ' + (MONTH_PAGE * MONTH_PAGES).toLocaleString() + ' events in these six weeks: '
                + 'the grid shows the first ' + (MONTH_PAGE * MONTH_PAGES).toLocaleString() + '. Choose one kind to narrow it.';
            note.hidden = false;
        } else {
            note.hidden = true;
        }
    }

    function goToMonth(y, m) {
        const d = new Date(y, m, 1);
        state.cursor = { y: d.getFullYear(), m: d.getMonth() };
    }
    function moveMonth(delta) {
        goToMonth(state.cursor.y, state.cursor.m + delta);
        // A day picked in another month is not on this grid.
        if (state.day && !gridDays(state.cursor.y, state.cursor.m).some((d) => dayKey(d) === state.day)) {
            state.day = null;
            renderList();
        }
        renderGrid();
        loadMonth();
    }
    function pickDay(k) {
        state.day = state.day === k ? null : k;
        renderGrid();
        renderList();
        if (state.active) showDetail(false);
    }

    // ── the list ─────────────────────────────────────────────────────────
    function listQuery(limit) {
        const now = new Date();
        if (state.filter === 'upcoming') {
            return { since: now.toISOString(), status: 'scheduled', order: 'asc', limit: limit };
        }
        if (state.filter === 'today') {
            return { since: startOfDay(now).toISOString(), until: endOfDay(now).toISOString(), order: 'asc',
                     limit: limit };
        }
        return { event_type: kindFilter(state.filter), order: 'desc', limit: limit };
    }

    // A further page continues after the last row already read, from that
    // row's own moment, rather than at an offset into a list that may have
    // moved meanwhile: an event that passed, ran or was cancelled since the
    // last page shifts every offset by one, and a row would be skipped. Rows
    // sharing that moment come in the route's own order (by row), so the ones
    // already read are skipped by count; any repeat is dropped by id.
    function after(shown, order) {
        const last = shown[shown.length - 1];
        const from = { offset: shown.filter((e) => e.event_at === last.event_at).length };
        from[order === 'asc' ? 'since' : 'until'] = last.event_at;
        return from;
    }
    function unseen(shown, rows) {
        const ids = new Set(shown.map((e) => e.event_id));
        return rows.filter((e) => !ids.has(e.event_id));
    }

    async function loadList(opts) {
        opts = opts || {};
        const seq = ++state.listSeq;
        const more = !!opts.more && state.list.next != null && state.list.events.length > 0;
        const keep = more ? state.list.events : [];
        // A refresh reads again as many rows as are on screen, so a list paged
        // down does not shrink under the person's eyes.
        const limit = opts.refresh ? Math.min(MONTH_PAGE, Math.max(PAGE, state.list.events.length)) : PAGE;
        const query = listQuery(limit);
        Object.assign(query, more ? after(keep, query.order) : { offset: 0 });
        try {
            const d = await json(fetch('/api/calendar/list?' + qs(query)));
            if (seq !== state.listSeq) return;
            state.list = { events: keep.concat(unseen(keep, d.events || [])), next: d.next_offset == null ? null : true,
                           error: null, loading: false };
        } catch (e) {
            if (seq !== state.listSeq) return;
            state.list = { events: keep, next: more ? state.list.next : null, error: e, loading: false };
        }
        renderList();
        stamp();
    }

    function emptyText() {
        if (state.day) return 'Nothing on this day.';
        if (state.filter === 'upcoming') return 'Nothing coming up. Add an event, or pick a day to see what was on it.';
        if (state.filter === 'today') return 'Nothing today.';
        if (state.filter === 'tasks') return 'Your Host has no scheduled task. Ask it to do something later, or on a schedule.';
        if (state.filter.indexOf('type:') === 0) return 'None of this kind.';
        return 'No events yet.';
    }

    function renderList() {
        const box = $('cal-list');
        clear(box);
        const dayMode = !!state.day;
        const clearDay = $('cal-day-clear');
        clearDay.hidden = !dayMode;
        let events;
        let error;
        if (dayMode) {
            events = state.month.events.filter((e) => { const d = when(e.event_at); return d && dayKey(d) === state.day; });
            error = state.month.error;
            clearDay.textContent = 'All days';
            const d = parseDayKey(state.day);
            box.appendChild(el('div', 'label-sys cal-day-head', d ? d.toLocaleDateString(undefined,
                { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }) : state.day));
        } else {
            events = state.list.events;
            error = state.list.error;
        }
        banner($('cal-banner'), error ? 'The calendar could not be read: ' + error.message : '', 'error');
        const loading = dayMode ? state.month.loading : state.list.loading;
        if (loading && !events.length) box.appendChild(el('p', 'cal-note', 'Reading the calendar…'));
        else if (!events.length && !error) box.appendChild(el('div', 'cseries-empty', emptyText()));
        let lastDay = null;
        events.forEach((e) => {
            const d = when(e.event_at);
            const k = d ? dayKey(d) : '';
            if (!dayMode && k !== lastDay) {
                box.appendChild(el('div', 'label-sys cal-day-head', d ? dayLabel(d) : 'No date'));
                lastDay = k;
            }
            box.appendChild(eventRow(e));
        });
        $('cal-more').hidden = dayMode || state.list.next == null;
    }

    function eventRow(e) {
        const row = button(null, 'cal-row');
        row.dataset.event = e.event_id;
        if (state.active && state.active.event_id === e.event_id) row.classList.add('active');
        // Title and state on the first line; what it is, how it repeats and
        // when on the second, the row's whole width.
        row.appendChild(el('span', 'cal-dot k-' + kindOf(e)));
        row.appendChild(el('span', 'cal-row-title', e.summary || '(no title)'));
        row.appendChild(pill(statusWord(e), statusTone(e)));
        const sub = [kindLabel(e)];
        const rep = repeats(e.recurrence);
        if (rep) sub.push(rep);
        const d = when(e.event_at);
        if (d) sub.push(timeOf(d) + ', ' + rel(d));
        row.appendChild(el('span', 'cal-row-sub', sub.join(' · ')));
        row.addEventListener('click', () => openEvent(e.event_id, { seed: e, focus: true }));
        return row;
    }

    function setFilter(f) {
        const kindChanged = kindFilter(f) !== kindFilter(state.filter);
        state.filter = f;
        $('cal-filter').value = f;
        state.day = null;
        state.list = { events: [], next: null, error: null, loading: true };
        renderGrid();
        renderList();
        loadList();
        if (kindChanged) loadMonth();
    }

    // ── one event in full ────────────────────────────────────────────────
    function showDetail(on) {
        $('cal-detail').hidden = !on;
        $('cal-list-view').hidden = on;
        $('cal-body').classList.toggle('detail-open', on);
        if (!on) {
            state.active = null;
            state.detailSeq++;
            renderList();
        }
    }

    async function openEvent(id, opts) {
        opts = opts || {};
        const seq = ++state.detailSeq;
        state.active = opts.seed || { event_id: id };
        state.runs = { runs: [], next: null, total: 0, loaded: false, error: null };
        state.rawOpen = false;
        state.openRuns = new Set();
        showDetail(true);
        banner($('cal-detail-banner'), '');
        renderDetail();
        if (opts.focus) $('cal-back').focus();
        // On a phone the one column scrolls; an event opens at its top.
        if (isPhone()) $('cal-body').scrollTop = 0;
        try {
            const full = await json(fetch('/api/calendar/read/' + encodeURIComponent(id)));
            if (seq !== state.detailSeq) return;
            state.active = full;
        } catch (e) {
            if (seq !== state.detailSeq) return;
            banner($('cal-detail-banner'), e.status === 404 ? 'This event is not on the calendar any more.'
                                                            : 'This event could not be read: ' + e.message, 'error');
            if (!opts.seed) state.active = null;
            renderDetail();
            return;
        }
        // A link to an event (view event:<id>) brings its month on screen.
        const d = when(state.active.event_at);
        if (opts.jump && d && (d.getFullYear() !== state.cursor.y || d.getMonth() !== state.cursor.m)) {
            goToMonth(d.getFullYear(), d.getMonth());
            renderGrid();
            loadMonth();
        }
        renderDetail();
        if (isTask(state.active)) loadRuns(false);
    }

    async function loadRuns(more, refresh) {
        const e = state.active;
        if (!e || !e.event_id) return;
        const seq = state.detailSeq;
        const keep = more ? state.runs.runs : [];
        const limit = refresh ? Math.min(500, Math.max(RUNS_PAGE, state.runs.runs.length)) : RUNS_PAGE;
        try {
            const d = await json(fetch('/api/calendar/events/' + encodeURIComponent(e.event_id) + '/runs?'
                                       + qs({ limit: limit, offset: more ? state.runs.next : 0 })));
            if (seq !== state.detailSeq) return;
            state.runs = { runs: keep.concat(d.runs || []), next: d.next_offset == null ? null : d.next_offset,
                           total: d.total || 0, loaded: true, error: null };
        } catch (err) {
            if (seq !== state.detailSeq) return;
            state.runs = Object.assign({}, state.runs, { loaded: true, error: err });
        }
        renderDetail();
    }

    function renderDetail() {
        const body = $('cal-detail-body');
        clear(body);
        const e = state.active;
        if (!e) return;
        if (!e.event_type) {           // only the id so far (a link); the read is on its way
            body.appendChild(el('p', 'cal-note', 'Reading the event…'));
            return;
        }
        body.appendChild(el('h2', 'cal-title', e.summary || '(no title)'));
        const pills = el('div', 'cal-pills');
        pills.appendChild(pill(kindLabel(e), isTask(e) ? 'dusk' : ''));
        if (e.status) pills.appendChild(pill(statusWord(e), statusTone(e)));
        if (isHeld(e)) pills.appendChild(el('span', 'cal-note', 'Runs with your next message.'));
        body.appendChild(pills);

        const facts = el('dl', 'cal-facts');
        function fact(label, value, cls) {
            if (value == null || value === '') return;
            facts.appendChild(el('dt', 'detail-label', label));
            facts.appendChild(el('dd', cls || null, value));
        }
        const p = (e.payload && typeof e.payload === 'object') ? e.payload : {};
        const d = when(e.event_at);
        const next = isTask(e) && e.status === 'scheduled';
        if (d) fact(next ? (p.wake_on ? 'At the latest' : 'Next run') : 'When', fullWhen(d) + ' (' + rel(d) + ')');
        fact('Repeats', repeats(e.recurrence));
        fact('Set by', setBy(e));
        if (isTask(e)) {
            if (p.wake_on && typeof p.wake_on === 'object') {
                const what = String(p.wake_on.event || 'it waits for').replace(/[_.]+/g, ' ');
                fact('Runs when', 'the event ' + what + ' happens, or at the time above');
            }
            if (p.op && typeof p.op === 'object' && p.op.tool) {
                fact('What it does', 'Runs one operation, ' + String(p.op.tool) + ', with no model.');
            } else if (typeof p.prompt === 'string' && p.prompt) {
                fact('What it does', p.prompt, 'cal-prose');
            }
            if (Array.isArray(p.steps) && p.steps.length) {
                fact('Steps', p.steps.map((s, i) => (i + 1) + '. ' + String(s)).join('\n'), 'cal-prose');
            }
        }
        if (e.status === 'cancelled' && e.cancelled_reason && !isHeld(e)) fact('Why cancelled', e.cancelled_reason);
        const cost = [];
        if (e.cost_usd > 0) cost.push('$' + Number(e.cost_usd).toFixed(4));
        if (e.tokens_in) cost.push(Number(e.tokens_in).toLocaleString() + ' tokens in');
        if (e.tokens_out) cost.push(Number(e.tokens_out).toLocaleString() + ' tokens out');
        if (e.model_id) cost.push(e.model_id);
        fact('Cost', cost.join(' · '));
        if (facts.firstChild) body.appendChild(facts);

        if (isTask(e)) body.appendChild(runsSection());

        if (Object.keys(p).length) {
            const raw = el('details', 'cal-raw');
            raw.open = state.rawOpen;
            raw.addEventListener('toggle', () => { state.rawOpen = raw.open; });
            raw.appendChild(el('summary', 'label-sys', 'Everything recorded on it'));
            raw.appendChild(el('pre', 'cal-raw-body', JSON.stringify(p, null, 2)));
            body.appendChild(raw);
        }

        const actions = el('div', 'cal-actions');
        if (isOpen(e) && !isTask(e)) {
            const done = button('Mark done', 'cseries-btn');
            done.id = 'cal-done';
            done.addEventListener('click', () => complete(e));
            actions.appendChild(done);
        }
        if (isOpen(e)) {
            const cancelBtn = button(isTask(e) ? 'Cancel task' : 'Cancel event', 'cseries-btn danger');
            cancelBtn.id = 'cal-cancel';
            cancelBtn.addEventListener('click', () => cancel(e));
            actions.appendChild(cancelBtn);
        }
        if (actions.firstChild) body.appendChild(actions);
    }

    function runsSection() {
        const r = state.runs;
        const box = el('section', 'cal-runs');
        box.setAttribute('aria-label', 'Runs');
        box.appendChild(el('div', 'label-sys', r.total ? 'Runs · ' + r.total.toLocaleString() : 'Runs'));
        if (r.error) box.appendChild(el('p', 'cal-note', 'The runs could not be read: ' + r.error.message));
        else if (!r.loaded) box.appendChild(el('p', 'cal-note', 'Reading the runs…'));
        else if (!r.runs.length) box.appendChild(el('p', 'cal-note', 'It has not run yet.'));
        // One line a run: its result, when, and the first line of what it said;
        // opening it shows all of what was kept.
        r.runs.forEach((run) => {
            const item = el('details', 'cal-run');
            // Keyed by when it ran, not its place: a new run at the top moves the rest down.
            const key = String(run.ran_at);
            item.open = state.openRuns.has(key);
            item.addEventListener('toggle', () => {
                if (item.open) state.openRuns.add(key);
                else state.openRuns.delete(key);
            });
            const head = el('summary', 'cal-run-head');
            head.appendChild(pill(run.ok ? 'ran' : 'failed', run.ok ? 'allow' : 'deny'));
            const d = when(run.ran_at);
            if (d) head.appendChild(el('span', 'cal-run-when', shortWhen(d) + ' · ' + rel(d)));
            const out = run.ok ? run.output_excerpt : (run.error || run.output_excerpt);
            if (out) head.appendChild(el('span', 'cal-run-line', String(out).trim().split('\n')[0]));
            item.appendChild(head);
            if (out) item.appendChild(el('pre', 'cal-run-out', out));
            box.appendChild(item);
        });
        if (r.next != null) {
            const older = button('Show older runs', 'cseries-btn ghost');
            older.id = 'cal-runs-more';
            older.addEventListener('click', () => loadRuns(true));
            box.appendChild(older);
        }
        return box;
    }

    // ── what the person can do to an event ───────────────────────────────
    async function afterChange(id) {
        await Promise.all([loadMonth(), loadList({ refresh: true })]);
        if (state.active && state.active.event_id === id) openEvent(id);
    }

    async function complete(e) {
        try {
            await json(fetch('/api/calendar/complete/' + encodeURIComponent(e.event_id), { method: 'POST' }));
        } catch (err) {
            banner($('cal-detail-banner'), 'It could not be marked done: ' + err.message, 'error');
            return;
        }
        afterChange(e.event_id);
    }

    async function cancel(e) {
        const task = isTask(e);
        const name = e.summary || (task ? 'this task' : 'this event');
        const reason = await ask({
            title: (task ? 'Cancel the task “' : 'Cancel “') + name + '”?',
            message: task
                ? 'Your Host will not run it again' + (repeats(e.recurrence) ? ', on any day' : '')
                  + '. If it is running now, it stops before its next step and does not report back.'
                : 'It stays on the calendar, marked cancelled.',
            input: true,
            placeholder: task ? 'Why, for your Host (optional)' : 'Why (optional)',
            ok: task ? 'Cancel task' : 'Cancel event',
        });
        if (reason === null) return;
        try {
            await json(fetch('/api/calendar/cancel/' + encodeURIComponent(e.event_id)
                             + (reason ? '?' + qs({ reason: reason }) : ''), { method: 'POST' }));
        } catch (err) {
            banner($('cal-detail-banner'), 'It could not be cancelled: ' + err.message, 'error');
            return;
        }
        afterChange(e.event_id);
    }

    // ── the confirm dialog (optionally with one text field) ─────────────
    function ask(opts) {
        return new Promise((resolve) => {
            const box = $('cal-confirm');
            const input = $('cal-c-input');
            $('cal-c-title').textContent = opts.title;
            $('cal-c-message').textContent = opts.message || '';
            input.hidden = !opts.input;
            input.value = '';
            input.placeholder = opts.placeholder || '';
            $('cal-c-ok').textContent = opts.ok || 'OK';
            const back = document.activeElement;
            box.hidden = false;
            (opts.input ? input : $('cal-c-ok')).focus();
            state.confirm = (ok) => {
                box.hidden = true;
                state.confirm = null;
                if (back && back.focus) back.focus();
                resolve(ok ? (opts.input ? input.value.trim() : '') : null);
            };
        });
    }

    // ── + Event ──────────────────────────────────────────────────────────
    function localInputValue(d) {
        return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
            + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    }
    function formError(text) {
        const n = $('cal-f-error');
        n.textContent = text || '';
        n.hidden = !text;
    }
    function openCompose() {
        const now = new Date();
        const picked = state.day ? parseDayKey(state.day) : null;
        // The picked day at nine, or the next whole hour from now.
        const start = picked && dayKey(picked) !== dayKey(now)
            ? new Date(picked.getFullYear(), picked.getMonth(), picked.getDate(), 9, 0)
            : new Date(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours() + 1, 0);
        $('cal-f-title').value = '';
        $('cal-f-when').value = localInputValue(start);
        $('cal-f-kind').value = 'event';
        $('cal-f-status').value = 'scheduled';
        formError('');
        $('cal-compose').hidden = false;
        $('cal-f-title').focus();
    }
    function closeCompose() {
        $('cal-compose').hidden = true;
        $('cal-new').focus();
    }
    async function saveCompose(ev) {
        ev.preventDefault();
        const save = $('cal-f-save');
        if (save.disabled) return;       // one Save, one event
        const title = $('cal-f-title').value.trim();
        if (!title) { formError('Give it a title.'); $('cal-f-title').focus(); return; }
        const raw = $('cal-f-when').value;
        // A datetime-local value with no offset is the person's own clock.
        const d = raw ? new Date(raw) : null;
        if (!d || isNaN(d.getTime())) { formError('Pick a day and a time.'); $('cal-f-when').focus(); return; }
        formError('');
        save.disabled = true;
        let made;
        try {
            made = await json(fetch('/api/calendar', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ event_type: $('cal-f-kind').value, summary: title, event_at: d.toISOString(),
                                       status: $('cal-f-status').value, source: 'user' }),
            }));
        } catch (err) {
            formError('It could not be saved: ' + err.message);
            save.disabled = false;
            return;
        }
        save.disabled = false;
        $('cal-compose').hidden = true;
        goToMonth(d.getFullYear(), d.getMonth());
        state.day = dayKey(d);
        renderGrid();
        await Promise.all([loadMonth(), loadList({ refresh: true })]);
        if (made && made.event_id) openEvent(made.event_id, { focus: true });
    }

    // ── a link into the app: #view=… on a new window, a message to an open one
    function viewFromHash() {
        const m = /(?:^|[#&])view=([^&]*)/.exec(window.location.hash || '');
        if (!m) return '';
        try { return decodeURIComponent(m[1]); } catch (e) { return ''; }
    }
    // event:<id> opens one event; day:YYYY-MM-DD picks a day; upcoming, today,
    // all and tasks choose the list.
    function applyView(v, booting) {
        v = String(v || '');
        if (v.indexOf('event:') === 0) {
            const id = v.slice(6);
            if (UUID_RE.test(id)) openEvent(id, { jump: true });
            return;
        }
        if (v.indexOf('day:') === 0) {
            const d = parseDayKey(v.slice(4));
            if (!d) return;
            goToMonth(d.getFullYear(), d.getMonth());
            state.day = dayKey(d);
            if (state.active) showDetail(false);
            if (!booting) { renderGrid(); renderList(); loadMonth(); }
            return;
        }
        if (FILTERS.indexOf(v) >= 0) {
            if (booting) { state.filter = v; $('cal-filter').value = v; } else setFilter(v);
        }
    }

    // ── refresh: a task's state moves while the window is open ───────────
    function refresh() {
        if (document.visibilityState !== 'visible') return;
        if (!$('cal-compose').hidden || !$('cal-confirm').hidden) return;
        loadMonth();
        loadList({ refresh: true });
        const e = state.active;
        if (e && e.event_id && e.event_type) {
            const seq = state.detailSeq;
            json(fetch('/api/calendar/read/' + encodeURIComponent(e.event_id))).then((full) => {
                if (seq !== state.detailSeq) return;
                state.active = full;
                renderDetail();
                if (isTask(full) && state.runs.loaded) loadRuns(false, true);
            }).catch(() => { /* the banner shows a failed open, not a failed refresh */ });
        }
    }

    // ── boot ─────────────────────────────────────────────────────────────
    function boot() {
        const now = new Date();
        state.cursor = { y: now.getFullYear(), m: now.getMonth() };
        $('cal-prev').addEventListener('click', () => moveMonth(-1));
        $('cal-next').addEventListener('click', () => moveMonth(1));
        $('cal-today').addEventListener('click', () => {
            const t = new Date();
            if (t.getFullYear() === state.cursor.y && t.getMonth() === state.cursor.m) return;
            goToMonth(t.getFullYear(), t.getMonth());
            moveMonth(0);
        });
        $('cal-filter').addEventListener('change', () => setFilter($('cal-filter').value));
        $('cal-day-clear').addEventListener('click', () => { if (state.day) pickDay(state.day); });
        $('cal-more').addEventListener('click', () => loadList({ more: true }));
        $('cal-back').addEventListener('click', () => showDetail(false));
        $('cal-new').addEventListener('click', openCompose);
        $('cal-f-cancel').addEventListener('click', closeCompose);
        $('cal-compose-form').addEventListener('submit', saveCompose);
        // A complaint about the form goes as soon as the person changes it.
        ['cal-f-title', 'cal-f-when'].forEach((id) => $(id).addEventListener('input', () => formError('')));
        $('cal-c-cancel').addEventListener('click', () => { if (state.confirm) state.confirm(false); });
        $('cal-c-ok').addEventListener('click', () => { if (state.confirm) state.confirm(true); });
        $('cal-c-input').addEventListener('keydown', (ev) => {
            if (ev.key === 'Enter' && state.confirm) { ev.preventDefault(); state.confirm(true); }
        });
        document.addEventListener('keydown', (ev) => {
            if (ev.key !== 'Escape') return;
            if (state.confirm) state.confirm(false);
            else if (!$('cal-compose').hidden) closeCompose();
            else if (!$('cal-detail').hidden) showDetail(false);
        });
        window.addEventListener('message', (ev) => {
            if (ev.source !== window.parent) return;
            const d = ev.data;
            if (d && d.type === 'cseries:app-view' && typeof d.view === 'string') applyView(d.view, false);
        });
        // A view set on a window already loaded, by its address alone.
        window.addEventListener('hashchange', () => {
            const v = viewFromHash();
            if (v) applyView(v, false);
        });
        document.addEventListener('visibilitychange', refresh);
        state.timer = setInterval(refresh, REFRESH_MS);

        const v = viewFromHash();
        if (v) applyView(v, true);
        renderGrid();
        renderList();
        loadMonth();
        loadList();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
    else boot();
})();
