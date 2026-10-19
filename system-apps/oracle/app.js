"use strict";

/* ================================================================
 * Memory — "The Ledger"
 * Bitemporal, records-first view of the Host's entries.
 *
 * v5: one Ledger lens (the Canonical/As-of/All picker IS the temporal
 * state — the old separate "Now" lens duplicated it), source-driven
 * Threads with a persistent list, a Corrections lens driven by the
 * real lineage columns, full-pane Health, and a purpose-built phone
 * layout (window.mobile: responsive — bottom tabs, master-detail,
 * inspector as a sheet). Intake/promotion moved to the Context app.
 * ================================================================ */

/* ---------- tiny helpers ---------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
const fmtNum = (n) => (Number(n) || 0).toLocaleString();

/* Phone breakpoint — matches the shell's md breakpoint (circles 0.6.0). */
const phoneMQ = window.matchMedia("(max-width: 768px)");
const isPhone = () => phoneMQ.matches;

function fmtDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d)) return "—";
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "2-digit" });
}
function fmtDayKey(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return "Undated";
    return d.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "2-digit" });
}
function fmtDayLabel(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return "UNDATED";
    const m = d.toLocaleDateString(undefined, { month: "short", day: "2-digit" }).toUpperCase();
    return `${m} · ${d.getFullYear()}`;
}
function fmtDateTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d)) return "—";
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "2-digit" })
        + " · " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}
function relTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso), now = Date.now(), diff = (now - d.getTime()) / 1000;
    if (isNaN(d)) return "—";
    if (diff < 60) return "just now";
    if (diff < 3600) return Math.floor(diff / 60) + "m ago";
    if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
    if (diff < 604800) return Math.floor(diff / 86400) + "d ago";
    return fmtDate(iso);
}

/* ---------- local timeline filter ----------
 * Each timeline lens gets its own filter box (the global search bar is a
 * different thing — it jumps to the Ledger). Filtering is client-side over
 * what's rendered: items whose visible text (textContent — exactly what
 * the user sees, uncapped) doesn't match hide, and any day group left
 * with nothing visible hides with them. */
function tlFilterHTML(placeholder) {
    return `<div class="tl-filter-row"><input class="cseries-input tl-filter" type="search"
        placeholder="${esc(placeholder)}" aria-label="${esc(placeholder)}">
        <span class="tl-filter-count cv-card-sub"></span></div>`;
}
function wireTlFilter(root, itemSel, groupSel) {
    const inp = root.querySelector(".tl-filter");
    if (!inp) return;
    const count = root.querySelector(".tl-filter-count");
    let deb;
    inp.addEventListener("input", () => {
        clearTimeout(deb);
        deb = setTimeout(() => {
            const q = inp.value.trim().toLowerCase();
            const items = $$(itemSel, root);
            let shown = 0;
            items.forEach((el) => {
                const hit = !q || el.textContent.toLowerCase().includes(q);
                el.hidden = !hit;
                if (hit) shown++;
            });
            if (groupSel) $$(groupSel, root).forEach((g) => {
                g.hidden = !$$(itemSel, g).some((el) => !el.hidden);
            });
            count.textContent = q ? `${shown} of ${items.length}` : "";
        }, 180);
    });
}

/* status → spine class + pill class */
const STATUS_SPINE = { active: "", corrected: "st-corrected", suppressed: "st-suppressed", archived: "st-archived" };
const STATUS_PILL = { active: "info", corrected: "dusk", suppressed: "deny", archived: "" };

/* ---------- network ---------- */
async function getJSON(url) {
    const r = await fetch(url, { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const j = await r.json();
    if (j && j.error) throw new Error(j.error);
    return j;
}
async function postJSON(url, body) {
    const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: body == null ? undefined : JSON.stringify(body),
    });
    if (!r.ok) {
        // Surface the server's detail (FastAPI puts actionable text there —
        // e.g. the 409 "no embedding model configured … set one via switch")
        // instead of a bare status code.
        const detail = await r.json().then((b) => b && (b.detail || b.error)).catch(() => null);
        throw new Error((typeof detail === "string" && detail) || ("HTTP " + r.status));
    }
    const j = await r.json();
    if (j && j.error) throw new Error(j.error);
    return j;
}

/* in-flight indicator (the one allowed breathing dot) */
let _inflight = 0;
function syncStart() { _inflight++; $("#sync-dot").classList.add("live"); }
function syncEnd() { _inflight = Math.max(0, _inflight - 1); if (!_inflight) $("#sync-dot").classList.remove("live"); }

/* ---------- app state ---------- */
const state = {
    lens: "ledger",         // ledger | threads | corrections | health | data
    temporal: "canonical",  // canonical | asof | all
    asOf: null,             // iso date string
    types: new Set(),       // selected memory_type filters
    status: null,           // selected single status facet
    selectedId: null,
    thread: null,           // selected thread {kind, ref} | null
    episode: null,          // selected conversation {started_at, ended_at} | null
};
const cache = { stats: null, coverage: null, rows: [], threads: [], episodes: [] };

/* ---------- lens metadata ---------- */
const LENS = {
    ledger: { title: "Ledger", sub: "The living log of entries — what your Host holds true, and how it got there." },
    threads: { title: "Threads", sub: "Memory grouped by where it happened — each app, project, and peer keeps its own." },
    corrections: { title: "Corrections", sub: "The Host changing its mind — every superseded entry and what replaced it." },
    health: { title: "Health", sub: "How memory forms, embedding coverage, and model readiness." },
    data: { title: "Data", sub: "What your Host keeps in its own database — each table's purpose and rows, and the snapshots you can go back to." },
};

/* thread-kind presentation (source-driven threading) */
const THREAD_KIND = {
    project: "Project",
    app: "App",
    peer: "Peer host",
    session: "Session",
};
function threadTitle(t) {
    if (!t) return "Thread";
    if (t.kind === "app") return t.ref;                       // "circles", "workbench"
    if (t.kind === "project") return "Project " + t.ref;
    if (t.kind === "peer") return t.ref;                      // peer / host handle
    return "Session " + String(t.ref || "").slice(0, 8);
}

/* ================================================================
 * Boot
 * ================================================================ */
window.addEventListener("DOMContentLoaded", () => {
    wireLensButtons("#rail .rail-btn");
    wireLensButtons("#tabbar .tab-btn");
    wireStatePicker();
    wireSearch();
    wireSectionToggles();
    wireScrim();
    phoneMQ.addEventListener("change", () => selectLens(state.lens));
    loadFacets();      // stats + coverage
    // Deep-link: #ledger / #threads / #corrections / #health opens that lens.
    const hashLens = (location.hash || "").slice(1);
    selectLens(LENS[hashLens] ? hashLens : "ledger");
});
window.addEventListener("hashchange", () => {
    const h = (location.hash || "").slice(1);
    if (LENS[h] && h !== state.lens) selectLens(h);
});

/* ---------- lens switching ---------- */
function wireLensButtons(sel) {
    $$(sel).forEach((btn) => {
        btn.addEventListener("click", () => selectLens(btn.dataset.lens));
    });
}
function selectLens(lens) {
    state.lens = lens;
    if (lens === "threads" && isPhone()) state.thread = null; // phone re-enters at the list
    $$("#rail .rail-btn, #tabbar .tab-btn").forEach((b) =>
        b.classList.toggle("active", b.dataset.lens === lens));
    const meta = LENS[lens] || LENS.ledger;
    $("#lens-title").textContent = meta.title;
    $("#lens-sub").textContent = meta.sub;
    closeInspector();
    closeFilterSheet();
    // totals strip only on ledger/corrections
    const showTotals = lens === "ledger" || lens === "corrections";
    $("#totals").hidden = !showTotals;
    // Sidebar: facets for the ledger, the thread list for threads,
    // nothing for corrections/health (they take the full pane). The
    // column collapses via a class on the shell — never display:none on
    // the grid item (auto-placement shifted #main into the 300px track).
    const sidebarMode = lens === "ledger" ? "facets" : (lens === "threads" ? "threads" : "none");
    $("#mem-app").classList.toggle("no-sidebar", sidebarMode === "none");
    $("#facets").hidden = sidebarMode !== "facets";
    $("#threads-panel").hidden = sidebarMode !== "threads";
    renderMainTools();
    if (showTotals) renderTotals();
    routeLens();
}
function routeLens() {
    switch (state.lens) {
        case "ledger": return loadLedger();
        case "threads": return loadThreads();
        case "corrections": return loadCorrections();
        case "health": return loadHealth();
        case "data": return loadData();
    }
}

/* ================================================================
 * Threads (source-driven threading) — memory grouped by where it
 * happened, then split into CONVERSATIONS by dormancy (30-min gap,
 * derived server-side at read time — nothing stamped on rows).
 * Desktop: the thread list lives in the sidebar and stays put while
 * the selected thread's conversations / turns fill the main pane.
 * Phone: master-detail — list → conversations → turns, with Back.
 * ================================================================ */
async function loadThreads() {
    syncStart();
    try {
        const j = await getJSON(`/api/brain/threads?limit=100`);
        cache.threads = j.threads || [];
    } catch (e) {
        cache.threads = null;   // null = load failed (vs [] = genuinely empty)
    } finally { syncEnd(); }

    if (cache.threads === null) {
        paneError($("#lens-body"), "Couldn't load threads.", loadThreads);
        $("#threads-list").innerHTML = "";
        return;
    }
    // Default-select the most recent thread so the pane is never empty.
    if (!state.thread && !isPhone() && cache.threads.length) {
        state.thread = { kind: cache.threads[0].kind, ref: cache.threads[0].ref };
    }
    renderThreadsSidebar();
    if (state.thread) loadThreadEpisodes(state.thread);
    else renderThreadListMain();
}

function threadRowHTML(t, compact) {
    const kindLabel = THREAD_KIND[t.kind] || t.kind || "—";
    const on = state.thread && state.thread.kind === t.kind && state.thread.ref === t.ref;
    return `<button class="thread-row ${on ? "on" : ""} ${compact ? "" : "thread-card"}"
        data-kind="${esc(t.kind)}" data-ref="${esc(t.ref)}">
        <div class="thread-card-top">
            <span class="thread-kind label-sys tk-${esc(t.kind)}">${esc(kindLabel)}</span>
            <span class="thread-when">${esc(relTime(t.last_activity))}</span>
        </div>
        <div class="thread-title">${esc(threadTitle(t))}</div>
        <div class="thread-counts">
            <span class="thread-count"><b>${fmtNum(t.turn_count)}</b> turns</span>
            <span class="thread-count"><b>${fmtNum(t.memory_count)}</b> memories</span>
        </div>
    </button>`;
}

function renderThreadsSidebar() {
    const host = $("#threads-list");
    $("#threads-count").textContent = cache.threads.length || "";
    if (!cache.threads.length) {
        host.innerHTML = `<span class="sb-mini">No threads yet.</span>`;
        return;
    }
    host.innerHTML = cache.threads.map((t) => threadRowHTML(t, true)).join("");
    $$("#threads-list .thread-row").forEach((el) => el.addEventListener("click", () => {
        state.thread = { kind: el.dataset.kind, ref: el.dataset.ref };
        state.episode = null;
        $$("#threads-list .thread-row").forEach((x) =>
            x.classList.toggle("on", x.dataset.kind === state.thread.kind && x.dataset.ref === state.thread.ref));
        loadThreadEpisodes(state.thread);
    }));
}

function renderThreadListMain() {
    const body = $("#lens-body");
    if (!cache.threads.length) {
        body.innerHTML = emptyState("No threads yet",
            "Threads form as you talk to your Host in different places — a Workbench project, Circles, a peer host. Each keeps its own memory.");
        return;
    }
    body.innerHTML = tlFilterHTML("Filter threads…") + `<div class="thread-grid">` +
        cache.threads.map((t) => threadRowHTML(t, false)).join("") + `</div>`;
    wireTlFilter(body, ".thread-row", null);
    $$("#lens-body .thread-row").forEach((el) => el.addEventListener("click", () => {
        state.thread = { kind: el.dataset.kind, ref: el.dataset.ref };
        state.episode = null;
        loadThreadEpisodes(state.thread);
    }));
}

/* Conversations within a thread — 30-min dormancy segments, server-derived */
async function loadThreadEpisodes(thread) {
    const body = $("#lens-body");
    body.innerHTML = skeletonRows(4);
    syncStart();
    try {
        const q = new URLSearchParams({ sctx_kind: thread.kind, sctx_ref: thread.ref });
        const j = await getJSON(`/api/brain/threads/episodes?${q.toString()}`);
        cache.episodes = j.episodes || [];
    } catch (e) {
        syncEnd();
        return paneError(body, "Couldn't load this thread: " + e.message,
            () => loadThreadEpisodes(thread));
    }
    syncEnd();
    // One conversation → no point in an extra layer; open it directly.
    if (cache.episodes.length === 1) {
        return loadEpisodeTurns(thread, cache.episodes[0]);
    }
    renderEpisodeList(thread);
}

function epTime(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return "—";
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function renderEpisodeList(thread) {
    const body = $("#lens-body");
    state.episode = null;
    $("#lens-title").textContent = threadTitle(thread);
    $("#lens-sub").textContent =
        `${cache.episodes.length} conversation${cache.episodes.length === 1 ? "" : "s"} — a new one starts after 30 quiet minutes.`;
    const back = isPhone()
        ? `<button class="cseries-btn ghost thread-back" id="thread-back">← All threads</button>` : "";
    if (!cache.episodes.length) {
        body.innerHTML = back + emptyState("No conversations yet", "This thread has no turns to show.");
    } else {
        // Timeline: conversations grouped under day markers, newest first
        // (server order), each with its exact time span — not a card grid.
        const groups = new Map();
        cache.episodes.forEach((ep, i) => {
            const key = fmtDayKey(ep.started_at);
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push(i);
        });
        let tl = "";
        for (const idxs of groups.values()) {
            const first = cache.episodes[idxs[0]];
            tl += `<div class="day-group"><div class="day-head"><span class="day-tick"></span>
                <span class="day-label label-sys">${esc(fmtDayLabel(first.started_at))}</span></div>
                <div class="day-rows">` + idxs.map((i) => {
                const ep = cache.episodes[i];
                return `<button class="ep-card" data-ep="${i}">
                    <span class="ep-time">${esc(epTime(ep.started_at))}–${esc(epTime(ep.ended_at))}</span>
                    <span class="ep-preview">${esc(ep.preview || "…")}</span>
                    <span class="ep-count">${fmtNum(ep.turn_count)} turns</span>
                </button>`;
            }).join("") + `</div></div>`;
        }
        body.innerHTML = back + tlFilterHTML("Filter conversations…") + tl;
        wireTlFilter(body, ".ep-card", ".day-group");
        $$("#lens-body .ep-card").forEach((el) => el.addEventListener("click", () =>
            loadEpisodeTurns(thread, cache.episodes[Number(el.dataset.ep)])));
    }
    const b = $("#thread-back");
    if (b) b.addEventListener("click", () => {
        state.thread = null; state.episode = null;
        $("#lens-title").textContent = LENS.threads.title;
        $("#lens-sub").textContent = LENS.threads.sub;
        renderThreadListMain();
    });
}

async function loadEpisodeTurns(thread, ep) {
    const body = $("#lens-body");
    body.innerHTML = skeletonRows(5);
    state.episode = ep;
    $("#lens-title").textContent = threadTitle(thread);
    $("#lens-sub").textContent =
        `${esc(fmtDate(ep.started_at))} ${epTime(ep.started_at)}–${epTime(ep.ended_at)} · ${fmtNum(ep.turn_count)} turns`;
    syncStart();
    try {
        const q = new URLSearchParams({
            sctx_kind: thread.kind, sctx_ref: thread.ref,
            since: ep.started_at, until: ep.ended_at,
            order: "asc", limit: "200",
        });
        const j = await getJSON(`/api/brain/chat?${q.toString()}`);
        renderThreadTurns(j.turns || [], thread);
    } catch (e) {
        paneError(body, "Couldn't load this conversation: " + e.message,
            () => loadEpisodeTurns(thread, ep));
    } finally { syncEnd(); }
}

function renderThreadTurns(turns, thread) {
    const body = $("#lens-body");
    // Back target: the conversation list when there are several; on the
    // phone a single-conversation thread backs out to the thread list
    // (the desktop sidebar list is always visible, no back needed there).
    const multi = cache.episodes.length > 1;
    const back = multi
        ? `<button class="cseries-btn ghost thread-back" id="thread-back">← Conversations</button>`
        : (isPhone() ? `<button class="cseries-btn ghost thread-back" id="thread-back">← All threads</button>` : "");
    if (!turns.length) {
        body.innerHTML = back + emptyState("No turns in range", "This conversation has no turns to show.");
    } else {
        // Chronological timeline: day markers + the exact clock time per
        // turn (relTime buried the order the moment turns were hours apart).
        const groups = new Map();
        turns.forEach((t) => {
            const key = fmtDayKey(t.created_at);
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push(t);
        });
        let tl = "";
        for (const grp of groups.values()) {
            tl += `<div class="day-group"><div class="day-head"><span class="day-tick"></span>
                <span class="day-label label-sys">${esc(fmtDayLabel(grp[0].created_at))}</span></div>
                <div class="day-rows thread-turns">` + grp.map((t) => {
                const role = (t.role || "user").toLowerCase();
                return `<div class="thread-turn tt-${esc(role)}">
                    <div class="tt-role label-sys">${esc(role)}<span class="tt-time">${esc(epTime(t.created_at))}</span></div>
                    <div class="tt-content">${esc(t.content)}</div>
                </div>`;
            }).join("") + `</div></div>`;
        }
        body.innerHTML = back + tlFilterHTML("Search this conversation…") + tl;
        wireTlFilter(body, ".thread-turn", ".day-group");
    }
    const b = $("#thread-back");
    if (b) b.addEventListener("click", () => {
        if (multi) { renderEpisodeList(thread); return; }
        state.thread = null; state.episode = null;
        $("#lens-title").textContent = LENS.threads.title;
        $("#lens-sub").textContent = LENS.threads.sub;
        renderThreadListMain();
    });
}

/* ---------- temporal state picker ---------- */
function wireStatePicker() {
    $$("#state-picker button").forEach((b) => {
        b.addEventListener("click", () => { applyTemporalUI(b.dataset.state); routeLens(); });
    });
    const di = $("#asof-date");
    let deb;
    di.addEventListener("input", () => {
        clearTimeout(deb);
        deb = setTimeout(() => { state.asOf = di.value || null; refreshAsOfCaption(); routeLens(); }, 350);
    });
}
/* Sync temporal state + picker chrome WITHOUT routing — callers decide
 * when to reload (search sets "all" and then runs its own fetch). */
function applyTemporalUI(t) {
    state.temporal = t;
    $$("#state-picker button").forEach((b) => {
        const on = b.dataset.state === t;
        b.classList.toggle("active", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    $("#asof-date").hidden = t !== "asof";
    if (t !== "asof") { state.asOf = null; }
    refreshAsOfCaption();
}
function refreshAsOfCaption() {
    const cap = $("#asof-caption");
    if (state.temporal === "asof" && state.asOf) {
        cap.hidden = false;
        cap.innerHTML = `<span class="ad"></span>Showing memory as it stood on ${esc(state.asOf)}`;
    } else {
        cap.hidden = true;
    }
}

/* ---------- search (single field, internal fallback) ---------- */
function wireSearch() {
    const inp = $("#search");
    let deb;
    inp.addEventListener("input", () => {
        clearTimeout(deb);
        const q = inp.value.trim();
        deb = setTimeout(() => runSearch(q), 320);
    });
}
async function runSearch(q) {
    if (!q) { routeLens(); return; }
    // search surfaces superseded entries too → flip to All + the ledger,
    // without the double reload the old setTemporal/selectLens pair caused
    if (state.temporal !== "all") applyTemporalUI("all");
    if (state.lens !== "ledger") {
        state.lens = "ledger";
        $$("#rail .rail-btn, #tabbar .tab-btn").forEach((b) =>
            b.classList.toggle("active", b.dataset.lens === "ledger"));
        $("#mem-app").classList.remove("no-sidebar");
        $("#facets").hidden = false;
        $("#threads-panel").hidden = true;
        $("#totals").hidden = false;
        renderMainTools();
    }
    const body = $("#lens-body");
    body.innerHTML = skeletonRows(5);
    syncStart();
    try {
        let rows;
        let armsNote = "";
        try {
            const sem = await getJSON(`/api/brain/memory/semantic?q=${encodeURIComponent(q)}&limit=50&include_superseded=true`);
            rows = (sem.results || []).map(normRow);
            // Retrieval-arms honesty (P4): say HOW the search ran — the route
            // reports its arms; words-only means the embedding lane is down.
            const arms = sem.arms || [];
            if (arms.includes("semantic") && arms.includes("lexical")) armsNote = "searched by meaning + words";
            else if (arms.includes("lexical")) armsNote = "searched by WORDS ONLY — meaning search is down";
            else if (arms.includes("semantic")) armsNote = "searched by meaning only";
        } catch (_) {
            // internal fallback: keyword
            const kw = await getJSON(`/api/brain/memory?q=${encodeURIComponent(q)}&limit=50`);
            rows = (kw.results || kw.memories || []).map(normRow);
            armsNote = "keyword fallback";
        }
        cache.rows = rows;
        $("#lens-title").textContent = `Search · "${q}"`;
        $("#lens-sub").textContent = `${rows.length} matching entr${rows.length === 1 ? "y" : "ies"} (${armsNote ? armsNote + "; " : ""}superseded surfaced, struck through).`;
        renderLedgerRows(rows);
    } catch (e) {
        paneError(body, "Search failed: " + e.message, () => runSearch(q));
    } finally { syncEnd(); }
}

/* ---------- section toggles ---------- */
function wireSectionToggles() {
    $$(".sb-section-head[data-toggle]").forEach((h) => {
        h.addEventListener("click", () => $("#" + h.dataset.toggle).classList.toggle("collapsed"));
    });
}

/* ---------- phone chrome: filter sheet + scrim ---------- */
function wireScrim() {
    $("#scrim").addEventListener("click", () => { closeInspector(); closeFilterSheet(); });
}
function syncScrim() {
    const open = isPhone() &&
        ($("#mem-app").classList.contains("inspector-open") ||
         $("#mem-app").classList.contains("sheet-open"));
    $("#scrim").hidden = !open;
}
function openFilterSheet() { $("#mem-app").classList.add("sheet-open"); syncScrim(); }
function closeFilterSheet() { $("#mem-app").classList.remove("sheet-open"); syncScrim(); }

/* ================================================================
 * Facets: stats + coverage
 * ================================================================ */
async function loadFacets() {
    syncStart();
    try {
        const [stats, cov] = await Promise.all([
            getJSON(`/api/brain/memories/stats`).catch(() => null),
            getJSON(`/api/brain/embed/coverage`).catch(() => null),
        ]);
        cache.stats = stats;
        cache.coverage = cov;
        renderTypeFacets(stats);
        renderStatusFacets(stats);
        if (state.lens === "ledger" || state.lens === "corrections") renderTotals();
    } catch (e) {
        // non-fatal — facets just stay minimal
    } finally { syncEnd(); }
}

function renderTypeFacets(stats) {
    const byType = (stats && stats.by_type) || {};
    const entries = Object.entries(byType).sort((a, b) => b[1] - a[1]);
    $("#types-count").textContent = entries.length ? entries.length : "";
    const host = $("#types-pills");
    if (!entries.length) { host.innerHTML = `<span class="sb-mini">No types yet.</span>`; return; }
    host.innerHTML = entries.map(([t, c]) =>
        `<span class="sb-pill" data-type="${esc(t)}">${esc(t)}<span class="pc">${fmtNum(c)}</span></span>`
    ).join("");
    $$("#types-pills .sb-pill").forEach((p) => p.addEventListener("click", () => {
        const t = p.dataset.type;
        if (state.types.has(t)) state.types.delete(t); else state.types.add(t);
        p.classList.toggle("on");
        routeLens();
    }));
}

function renderStatusFacets(stats) {
    const byStatus = (stats && stats.by_status) || {};
    const order = ["active", "corrected", "archived", "suppressed"];
    const keys = Object.keys(byStatus).sort((a, b) => order.indexOf(a) - order.indexOf(b));
    const host = $("#status-pills");
    if (!keys.length) { host.innerHTML = `<span class="sb-mini">No status data.</span>`; return; }
    host.innerHTML = keys.map((s) =>
        `<span class="sb-pill s-${esc(s)}" data-status="${esc(s)}">${esc(s)}<span class="pc">${fmtNum(byStatus[s])}</span></span>`
    ).join("");
    $$("#status-pills .sb-pill").forEach((p) => p.addEventListener("click", () => {
        const s = p.dataset.status;
        if (state.status === s) { state.status = null; p.classList.remove("on"); }
        else {
            state.status = s;
            $$("#status-pills .sb-pill").forEach((x) => x.classList.remove("on"));
            p.classList.add("on");
        }
        routeLens();
    }));
}

/* ---------- main toolbar (per lens) ---------- */
function renderMainTools() {
    const host = $("#main-tools");
    let html = "";
    if (state.lens !== "health") {
        html += `<button class="cseries-btn ghost" id="refresh-btn" title="Reload">↻ Refresh</button>`;
    }
    // Phone: facets live in a slide-over sheet; the ledger gets a Filters button.
    if (state.lens === "ledger") {
        const n = state.types.size + (state.status ? 1 : 0) + (state.temporal !== "canonical" ? 1 : 0);
        html += `<button class="cseries-btn phone-only" id="filters-btn">Filters${n ? ` · ${n}` : ""}</button>`;
    }
    host.innerHTML = html;
    const rb = $("#refresh-btn");
    if (rb) rb.addEventListener("click", () => { loadFacets(); routeLens(); });
    const fb = $("#filters-btn");
    if (fb) fb.addEventListener("click", openFilterSheet);
}

/* ================================================================
 * Totals strip — every number here is real and server-counted:
 * stats.superseded / stats.open_goals landed with this version.
 * ================================================================ */
function renderTotals() {
    const st = cache.stats, cov = cache.coverage;
    const totals = $("#totals");
    if (!st && !cov) { totals.innerHTML = `<div class="cv-card"><div class="cv-card-label">Loading…</div></div>`; return; }
    // Live entries = active minus raw chat turns (turns live in Threads)
    const turns = st && st.by_type ? (st.by_type.turn || 0) : 0;
    const liveEntries = st ? Math.max(0, (st.total_active || 0) - turns) : "—";
    const superseded = st && st.superseded != null ? fmtNum(st.superseded) : "—";
    const openGoals = st && st.open_goals != null ? fmtNum(st.open_goals) : "—";
    // searchable % — honest: embedding coverage, never "dimensioned"
    let searchable = "—";
    if (cov && cov.entity_memory && cov.entity_memory.total) {
        searchable = Math.round(cov.entity_memory.embedded / cov.entity_memory.total * 100) + "%";
    } else if (st && st.embedding_coverage) {
        searchable = st.embedding_coverage.coverage_pct + "%";
    }
    const embeddedN = cov && cov.entity_memory ? cov.entity_memory.embedded : (st && st.embedding_coverage ? st.embedding_coverage.embedded : 0);
    const totalN = cov && cov.entity_memory ? cov.entity_memory.total : (st && st.embedding_coverage ? st.embedding_coverage.total : 0);

    totals.innerHTML = `
        <div class="cv-card">
            <div class="cv-card-label">Live entries</div>
            <div class="cv-card-value">${fmtNum(liveEntries)}</div>
            <div class="cv-card-sub">canonical, in force now</div>
        </div>
        <div class="cv-card cv-card-accent">
            <div class="cv-card-label">Superseded</div>
            <div class="cv-card-value">${superseded}</div>
            <div class="cv-card-sub">${superseded !== "—" && superseded !== "0" ? "the Host changed its mind" : "no revisions yet"}</div>
        </div>
        <div class="cv-card">
            <div class="cv-card-label">Open goals</div>
            <div class="cv-card-value">${openGoals}</div>
            <div class="cv-card-sub">goal + unresolved, awaiting resolution</div>
        </div>
        <div class="cv-card">
            <div class="cv-card-label">Searchable</div>
            <div class="cv-card-value">${searchable}</div>
            <div class="cv-card-sub">${fmtNum(embeddedN)} / ${fmtNum(totalN)} embedded</div>
        </div>`;
}

/* ================================================================
 * Ledger lens
 * ================================================================ */
function buildLedgerQuery() {
    const p = new URLSearchParams();
    // Canonical = live entries. All / As-of = the full record (status
    // filters would wrongly hide rows whose CURRENT status changed later).
    if (state.temporal === "canonical") p.set("status", "active");
    else p.set("status", "all");
    if (state.status) p.set("status", state.status);
    if (state.types.size) {
        p.set("memory_type", Array.from(state.types).join(","));
    } else if (cache.stats && cache.stats.by_type) {
        // No explicit type filter → every type EXCEPT raw chat turns.
        // Turns are the Threads lens's material; mixed in here they bury
        // the distilled entries (they're ~40% of the store).
        const t = Object.keys(cache.stats.by_type).filter((k) => k !== "turn");
        if (t.length) p.set("memory_type", t.join(","));
    }
    if (state.temporal === "all") p.set("include_superseded", "true");
    if (state.temporal === "asof" && state.asOf) {
        p.set("as_of", state.asOf);
        p.set("include_superseded", "true");
    }
    p.set("limit", "200");
    return p.toString();
}

async function loadLedger() {
    const body = $("#lens-body");
    body.innerHTML = skeletonRows(6);
    syncStart();
    try {
        const j = await getJSON(`/api/brain/memories?${buildLedgerQuery()}`);
        const rows = (j.memories || []).map(normRow);
        cache.rows = rows;
        renderTotals();
        renderLedgerRows(rows);
    } catch (e) {
        paneError(body, "Couldn't load the ledger: " + e.message, loadLedger);
    } finally { syncEnd(); }
}

function normRow(r) {
    return {
        id: r.id,
        content: r.content || "",
        memory_type: r.memory_type || "fact",
        status: r.status || "active",
        importance: r.importance != null ? r.importance : 1,
        confidence: r.confidence != null ? r.confidence : 1,
        access_count: r.access_count || 0,
        created_at: r.created_at || null,
        t_valid_from: r.t_valid_from || null,
        t_valid_until: r.t_valid_until || null,
        resolution_pointer: r.resolution_pointer != null ? r.resolution_pointer : null,
        corrected_by: r.corrected_by != null ? r.corrected_by : null,
        correction_of: r.correction_of != null ? r.correction_of : null,
        resolved_at: r.resolved_at || null,
        status_reason: r.status_reason || null,
        similarity: r.similarity != null ? r.similarity : null,
        metadata: r.metadata || {},
    };
}

function renderLedgerRows(rows) {
    const body = $("#lens-body");
    if (!rows.length) {
        body.innerHTML = emptyState("No entries here",
            "No live entries yet — chat with your Host, or promote context from the Context app, to start the ledger.");
        return;
    }
    // group by day
    const groups = new Map();
    rows.forEach((r) => {
        const key = r.created_at ? fmtDayKey(r.created_at) : "Undated";
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(r);
    });
    let html = "";
    for (const [key, grp] of groups) {
        const label = grp[0].created_at ? fmtDayLabel(grp[0].created_at) : "UNDATED";
        html += `<div class="day-group"><div class="day-head"><span class="day-tick"></span>
            <span class="day-label label-sys">${esc(label)}</span></div>
            <div class="day-rows">${grp.map(rowHTML).join("")}</div></div>`;
    }
    body.innerHTML = html;
    $$("#lens-body .mem-row").forEach((el) => el.addEventListener("click", () => {
        openInspector(Number(el.dataset.id));
    }));
}

function rowHTML(r) {
    const spineCls = STATUS_SPINE[r.status] || "";
    const superseded = r.t_valid_until && r.status === "active";
    const cls = ["mem-row", spineCls, superseded ? "st-superseded" : "", r.id === state.selectedId ? "selected" : ""]
        .filter(Boolean).join(" ");
    const pill = STATUS_PILL[r.status] || "";
    const impPct = Math.round(Math.max(0, Math.min(1, r.importance)) * 100);
    const confPct = Math.round(Math.max(0, Math.min(1, r.confidence)) * 100);
    const reason = (r.status === "corrected" || r.status === "suppressed") && r.status_reason
        ? `<span class="mr-reason">${esc(r.status_reason)}</span>` : "";
    // goal / unresolved entries carry their resolution state on the row
    const openGoal = (r.memory_type === "goal" || r.memory_type === "unresolved");
    const goalChip = openGoal
        ? (r.resolved_at
            ? `<span class="cseries-pill allow mr-goal">resolved</span>`
            : `<span class="cseries-pill warn mr-goal">open</span>`)
        : "";
    // A rule (taught or learned) is a preference marked rule_kind: say so. A
    // skill_memory row is a stored skill, not a rule.
    const ruleChip = (r.memory_type === "preference" && r.metadata && r.metadata.rule_kind)
        ? `<span class="cseries-pill info mr-goal">${esc(r.metadata.rule_kind)} rule</span>`
        : (r.memory_type === "skill_memory" ? `<span class="cseries-pill mr-goal">stored skill</span>` : "");
    const hasHistory = r.corrected_by || r.resolution_pointer || r.correction_of;
    const superMark = hasHistory ? `<span class="mr-super" title="part of a correction chain">⌃</span>` : "";
    const access = r.access_count ? `↺ ${fmtNum(r.access_count)}×` : "";
    return `<div class="${cls}" data-id="${r.id}">
        <span class="cseries-pill ${pill} mr-type">${esc(r.memory_type)}</span>
        <span class="mr-content">${esc(r.content)}${reason}</span>
        ${goalChip}${ruleChip}
        <span class="mr-bars">
            <span class="cv-rate-bar" title="importance ${impPct}%"><span style="width:${impPct}%"></span></span>
            <span class="cv-rate-bar bar-conf" title="confidence ${confPct}%"><span style="width:${confPct}%"></span></span>
        </span>
        <span class="mr-access">${esc(access)}</span>
        <span class="mr-date">${esc(fmtDate(r.created_at))}</span>
        ${superMark}
    </div>`;
}

/* ================================================================
 * Corrections lens — server-filtered to lineage rows (lineage_only),
 * chains walked old → new via the real pointer columns.
 * ================================================================ */
async function loadCorrections() {
    const body = $("#lens-body");
    body.innerHTML = skeletonRows(5);
    syncStart();
    try {
        // lineage_only: rows superseded, corrected, or linked either way.
        // status=all — a corrected row's CURRENT status is 'corrected', the
        // old default (status=active) filtered the whole lens empty.
        const j = await getJSON(`/api/brain/memories?status=all&include_superseded=true&lineage_only=true&limit=200`);
        const rows = (j.memories || []).map(normRow);
        cache.rows = rows;
        renderTotals();
        if (!rows.length) {
            body.innerHTML = emptyState("No corrections yet",
                "When your Host revises an entry — you correct it in conversation and it curates, or you edit it here — the supersession chain shows up in this lens.");
            return;
        }
        const byId = new Map(rows.map((r) => [r.id, r]));
        // Chain partners can be plain rows (a consolidation replacement has
        // no lineage column of its own) — fetch the missing ends, bounded.
        const missing = new Set();
        rows.forEach((r) => {
            [r.corrected_by, r.resolution_pointer, r.correction_of].forEach((id) => {
                if (id != null && !byId.has(id)) missing.add(id);
            });
        });
        const fetched = await Promise.all(Array.from(missing).slice(0, 40).map((id) =>
            getJSON(`/api/brain/memories/${id}`).then(normRow).catch(() => null)));
        fetched.filter(Boolean).forEach((r) => byId.set(r.id, r));

        // forward pointer: what replaced this row
        const fwd = (r) => r.corrected_by || r.resolution_pointer || null;
        const pointedTo = new Set();
        byId.forEach((r) => { const f = fwd(r); if (f != null) pointedTo.add(f); });
        // roots = lineage rows nothing points at; walk forward to the head
        const seen = new Set();
        const chains = [];
        rows.forEach((r) => {
            if (seen.has(r.id) || pointedTo.has(r.id)) return;
            const chain = [];
            let cur = r; const guard = new Set();
            while (cur && !guard.has(cur.id)) {
                guard.add(cur.id); seen.add(cur.id); chain.push(cur);
                const nxt = fwd(cur);
                cur = nxt != null ? byId.get(nxt) : null;
            }
            chains.push(chain);
        });
        // Most recently revised chain first — "when did the Host last change
        // its mind here" is the sort key, not when the chain started.
        const chainStamp = (chain) => Math.max(...chain.map((n) =>
            Math.max(Date.parse(n.created_at) || 0, Date.parse(n.t_valid_until) || 0)));
        chains.sort((a, b) => chainStamp(b) - chainStamp(a));

        body.innerHTML = tlFilterHTML("Filter corrections…") + chains.map((chain) => {
            const nodes = chain.map((n, i) => {
                const head = i === chain.length - 1 && !n.t_valid_until; // newest live = canonical
                const next = chain[i + 1] || null;
                // Three real moments, each with time: when the original was
                // decided (created_at of node 0), when a revision was made
                // (created_at of the replacement), and when the old row was
                // superseded (its t_valid_until, stamped at correction time).
                const when = `<div class="corr-when label-sys">${i === 0 ? "Decided" : "Corrected"} ${esc(fmtDateTime(n.created_at))}${head ? " — in force now" : ""}</div>`;
                // "retired, no replacement" only when the row genuinely has
                // no forward pointer — a chain can also end because the
                // partner row failed to fetch, and that must not read as a
                // deliberate retirement.
                const fptr = fwd(n);
                const superLine = n.t_valid_until
                    ? `<div class="corr-super label-sys">Superseded ${esc(fmtDateTime(n.t_valid_until))}${next ? ` by #${next.id}` : (fptr != null ? ` by #${fptr}` : " — retired, no replacement")}</div>` : "";
                const cls = "corr-node" + (head ? " head" : "");
                return `<div class="${cls}">${when}<div class="mem-row ${head ? "" : "st-superseded"}" data-id="${n.id}">
                    <span class="cseries-pill ${head ? "info" : ""} mr-type">${esc(n.memory_type)}</span>
                    <span class="mr-content ${head ? "" : "struck"}">${esc(n.content)}</span>
                </div>${superLine}</div>`;
            }).join("");
            return `<div class="corr-chain">${nodes}</div>`;
        }).join("");
        wireTlFilter(body, ".corr-chain", null);
        $$("#lens-body .mem-row").forEach((el) => el.addEventListener("click", () => openInspector(Number(el.dataset.id))));
    } catch (e) {
        paneError(body, "Couldn't load corrections: " + e.message, loadCorrections);
    } finally { syncEnd(); }
}

/* ================================================================
 * Health lens — how memory forms, coverage, model readiness.
 * Full pane: no sidebar, cards flow in a responsive grid.
 * ================================================================ */

/* Is memory still FORMING? — the question the Health lens could not answer.
 *
 * Everything else on this lens describes the store's CONTENTS. None of it tells
 * you whether the pipeline that fills the store is still running: once
 * fact derivation died and every count here stayed plausible for 16 days.
 *
 * Reads `formation` from /api/brain/memories/stats, where staleness is computed
 * by the SERVER from the newest successful pass — not self-reported by the
 * worker, because a worker that died cannot report that it died.
 *
 * Three states, and "unknown" is deliberately not folded into "fine": a backend
 * without the ledger says so rather than showing a reassuring green card. */
function formationCard(f) {
    if (!f || !f.available) {
        const why = (f && f.reason) ? ` — ${esc(f.reason)}` : "";
        return `<div class="cseries-card subtle" style="margin-bottom:14px">
            <div class="card-title">Memory formation</div>
            <div class="card-sub">Can't tell yet${why}. Nothing has recorded
            whether your Host is still turning conversations into memories, so
            this is unknown — not confirmed working.</div></div>`;
    }
    // Off by design: memory forms in the turn, so there is no pass to be late.
    if (f.in_turn) {
        return `<div class="cseries-card subtle" style="margin-bottom:14px">
            <div class="card-title">Memory formation — in the turn</div>
            <div class="card-sub">Your Host writes what it learns as it answers:
            what you want, the thread's goal, and where the next turn picks up.
            Nothing re-reads your conversations afterwards.</div></div>`;
    }
    const mins = (s) => (s === null || s === undefined) ? null
        : (s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)}h`);
    const ago = mins(f.seconds_since_success);
    const queueBits = [];
    // -1 is UNKNOWN, not zero — say so rather than printing a confident 0.
    if (f.turn_queue > 0) queueBits.push(`${fmtNum(f.turn_queue)} conversation turn(s) waiting`);
    if (f.turn_queue < 0) queueBits.push(`turn queue unreadable`);
    if (f.graph_queue > 0) queueBits.push(`${fmtNum(f.graph_queue)} entry(s) waiting to be linked`);
    if (f.graph_queue < 0) queueBits.push(`link queue unreadable`);
    // An idle Host with nothing queued is HEALTHY, not suspicious: the check
    // runs on its own timer whether or not you've been talking, so "nothing
    // waiting" is the normal resting state and should read that way.
    const queueLine = queueBits.length
        ? queueBits.join(" · ")
        : "Nothing waiting — everything you've said has been taken in.";

    // Just started (fresh boot, or waking from idle). It has not had time to
    // complete a pass yet, which is not the same as not running.
    if (f.warming_up && f.stale !== true) {
        return `<div class="cseries-card subtle" style="margin-bottom:14px">
            <div class="card-title">Memory formation — starting up</div>
            <div class="card-sub">Your Host just came online. The first check
            runs within a minute.</div></div>`;
    }
    if (f.stale) {
        const detail = ago
            ? `The last successful pass was ${ago} ago.`
            : `No successful pass has ever been recorded on this host.`;
        const err = f.last_error
            ? `<div class="card-sub" style="margin-top:6px"><b>Last reported reason:</b> ${esc(f.last_error)}</div>`
            : "";
        return `<div class="cseries-card subtle" style="margin-bottom:14px;border-left:3px solid #e67e22">
            <div class="card-title">Memory formation — needs attention</div>
            <div class="card-sub">${detail} New conversations may not be turning
            into memories. ${esc(queueLine)}</div>${err}</div>`;
    }
    const probs = f.problems_24h
        ? ` ${f.problems_24h} pass(es) reported a problem in the last 24 hours.`
        : "";
    return `<div class="cseries-card subtle" style="margin-bottom:14px">
        <div class="card-title">Memory formation — running</div>
        <div class="card-sub">Last completed ${ago ? ago + " ago" : "just now"}.
        ${esc(queueLine)}${probs}</div></div>`;
}

async function loadHealth() {
    const body = $("#lens-body");
    body.innerHTML = skeletonRows(3);
    syncStart();
    try {
        const [cov, bm, stats] = await Promise.all([
            getJSON(`/api/brain/embed/coverage`),
            getJSON(`/api/brain/embed/coverage/by-model`).catch(() => ({ models: [], current_model: null })),
            // Formation state. Older backends have no `formation` key at all —
            // treat that as UNKNOWN, never as healthy.
            getJSON(`/api/brain/memories/stats`).catch(() => ({})),
        ]);
        const em = cov.entity_memory || { total: 0, embedded: 0, missing: 0 };
        const uc = cov.user_context || { total: 0, embedded: 0, missing: 0 };
        const emPct = em.total ? Math.round(em.embedded / em.total * 100) : 0;
        const ucPct = uc.total ? Math.round(uc.embedded / uc.total * 100) : 0;
        body.innerHTML = `${formationCard(stats.formation)}<div class="health-grid">
            <div class="cseries-card subtle">
                <div class="card-title">Embedding coverage</div>
                <div class="cv-card-label" style="margin-top:8px">Host memory</div>
                <div class="cov-bar-wrap"><div class="cov-bar"><span style="width:${emPct}%"></span></div>
                <div class="cov-legend"><span>${fmtNum(em.embedded)} searchable</span><span>${fmtNum(em.missing)} missing</span></div></div>
                <div class="cv-card-label" style="margin-top:14px">User context (intake)</div>
                <div class="cov-bar-wrap"><div class="cov-bar"><span style="width:${ucPct}%"></span></div>
                <div class="cov-legend"><span>${fmtNum(uc.embedded)} embedded</span><span>${fmtNum(uc.missing)} missing</span></div></div>
                <div class="cv-card-sub" style="margin-top:10px">${cov.active_jobs ? cov.active_jobs + " embed job(s) running now." : "No embed jobs running."}</div>
            </div>

            <div class="cseries-card subtle">
                <div class="card-title">Embedding models</div>
                <div class="card-sub" style="margin-bottom:10px">Which models your memory is ready to search with. Models live side by side — each keeps its own vector column, so switching is a pointer change: nothing is re-embedded, migrated, or lost. A model with missing coverage embeds only its missing rows, into its own column.</div>
                <div id="model-readiness">${renderModelRows(bm.models)}</div>
            </div>
        </div>`;
        $$("#model-readiness .mr-switch").forEach((b) =>
            b.addEventListener("click", () => switchEmbeddingModel(b.dataset.model, b)));
        $$("#model-readiness .mr-fill").forEach((b) =>
            b.addEventListener("click", () => enqueueFillJob(b)));
    } catch (e) {
        paneError(body, "Couldn't load health: " + e.message, loadHealth);
    } finally { syncEnd(); }
}

function renderModelRows(models) {
    if (!models || !models.length) {
        return `<div class="cv-card-sub">No embedding catalog right now — the platform bridge may be offline. Coverage above still reflects your active model.</div>`;
    }
    return models.map((m) => {
        const pct = m.total ? Math.round(m.embedded / m.total * 100) : 0;
        let tag;
        if (m.is_current) tag = `<span class="cseries-pill allow em-tag">Active</span>`;
        else if (m.ready) tag = `<span class="cseries-pill info em-tag">Ready</span>`;
        else if (m.embedded) tag = `<span class="cseries-pill warn em-tag">${pct}% ready</span>`;
        else tag = `<span class="cseries-pill deny em-tag">Not embedded</span>`;
        const rate = (m.charge_microdollars_per_M != null)
            ? `$${(m.charge_microdollars_per_M / 1e6).toFixed(3)}/M tok` : "";
        const missing = Math.max(0, (m.total || 0) - (m.embedded || 0));
        // Actions are ADD-ONLY: a fill job embeds ONLY the
        // rows whose cell in this model's own dim column is empty — never a
        // bulk re-embed. `fillable` is what a fill can actually reach; a gap
        // beyond that is same-dim-rival rows sharing the physical column,
        // which only a re-embed converts — a consented host action, never a
        // button here (never-migrate-vectors doctrine). So: fillable gap →
        // fill button; unfillable remainder → say "needs re-embed", offer
        // nothing that would queue 0-row jobs.
        const fillable = Math.min(missing, m.fillable || 0);
        const stuck = missing - fillable;
        const fillBtn = fillable
            ? `<button class="cseries-btn mr-fill" data-model="${esc(m.model)}">Embed missing (${fmtNum(fillable)})</button>` : "";
        const stuckNote = stuck
            ? `<span class="cv-card-sub">${fmtNum(stuck)} rows hold a same-dim model's vectors — ask your host for a re-embed (overwrites those vectors) if you want them on this model.</span>` : "";
        const btn = m.is_current
            ? (fillBtn || (missing ? "" : `<button class="cseries-btn" disabled>In use</button>`))
            : `<button class="cseries-btn mr-switch" data-model="${esc(m.model)}" data-fillable="${fillable}" data-missing="${missing}">${(m.ready || !missing) ? "Use" : (fillable ? "Use + embed missing" : "Use")}</button>`;
        return `<div class="em-row">
            <div class="em-row-head">
                <span class="em-name">${esc(m.model)}</span>
                <span class="cv-card-sub em-dim">${m.dim}d · ${esc(m.provider || "privatae")}</span>
                ${tag}
            </div>
            <div class="cov-bar-wrap"><div class="cov-bar"><span style="width:${pct}%"></span></div>
            <div class="cov-legend"><span>${fmtNum(m.embedded)}/${fmtNum(m.total)} embedded</span><span>${rate}</span></div></div>
            <div class="em-row-act">${btn}${stuckNote}</div>
        </div>`;
    }).join("");
}

async function enqueueFillJob(btn) {
    const orig = btn.textContent;
    btn.disabled = true; btn.textContent = "Queuing…";
    syncStart();
    try {
        const job = await postJSON(`/api/brain/embed/jobs`, { target: "entity_memory" });
        if (job && job.error) throw new Error(job.error);
        banner("success",
            `Fill job #${job.id} queued — embedding ${fmtNum(job.total)} missing rows into this model's own column. Nothing existing is touched.`);
        loadHealth();
    } catch (e) {
        banner("error", "Couldn't queue the fill job: " + e.message);
        btn.disabled = false; btn.textContent = orig;
    } finally { syncEnd(); }
}

async function switchEmbeddingModel(model, btn) {
    if (!model) return;
    const orig = btn.textContent;
    btn.disabled = true; btn.textContent = "Switching…";
    syncStart();
    try {
        const res = await postJSON(`/api/embedding/switch`, { model });
        if (res && res.error) throw new Error(res.error);
        // Per-model counts from the row we rendered (data attrs), NOT the
        // server's rows_missing_active_dim: that counts column-NULL cells
        // only, which reads 0 for a same-dim switch and would falsely claim
        // "fully covered" while the model row shows a real gap.
        const fillable = +(btn.dataset.fillable || 0);
        const stuck = Math.max(0, +(btn.dataset.missing || 0) - fillable);
        const stuckTail = stuck
            ? ` ${fmtNum(stuck)} rows hold a same-dim model's vectors — searching them with ${model} is degraded until a host-run re-embed.` : "";
        if (fillable) {
            // "Use + embed missing" keeps its promise: switch, then queue the
            // add-only fills for this model's own column — both banks, no
            // second click, and never a re-embed of existing vectors. The
            // user_context fill is a no-op when that bank has no gap (409s
            // cleanly when no context was ever ingested).
            const job = await postJSON(`/api/brain/embed/jobs`, { target: "entity_memory" }).catch((e) => ({ error: e.message }));
            await postJSON(`/api/brain/embed/jobs`, { target: "user_context" }).catch(() => null);
            banner(job && !job.error ? "success" : "info", (job && !job.error)
                ? `Now embedding with ${model}. Fill job #${job.id} queued — ${fmtNum(fillable)} rows embedding into this model's own column; existing vectors untouched.${stuckTail}`
                : `Now embedding with ${model}. ${fmtNum(fillable)} rows still need vectors on this model, but the fill job didn't queue (${(job && job.error) || "unknown"}) — use Embed missing on the model row.${stuckTail}`);
        } else if (stuck) {
            banner("info", `Now embedding with ${model}.${stuckTail}`);
        } else {
            banner("success", `Now embedding with ${model} — already fully covered on this model.`);
        }
        loadHealth();
    } catch (e) {
        banner("error", "Switch failed: " + e.message);
        btn.disabled = false; btn.textContent = orig;
    } finally { syncEnd(); }
}

/* ================================================================
 * Inspector — "the Record"
 * ================================================================ */
async function openInspector(id) {
    if (!id) return;
    state.selectedId = id;
    $$("#lens-body .mem-row").forEach((el) => el.classList.toggle("selected", Number(el.dataset.id) === id));
    const ins = $("#inspector");
    $("#mem-app").classList.add("inspector-open");
    ins.setAttribute("aria-hidden", "false");
    syncScrim();
    ins.innerHTML = `<div class="ins-header"><span class="ins-title">Loading…</span>
        <button class="ins-close" aria-label="Close">×</button></div>
        <div class="ins-body">${skeletonRows(4)}</div>`;
    ins.querySelector(".ins-close").addEventListener("click", closeInspector);
    syncStart();
    try {
        const m = await getJSON(`/api/brain/memories/${id}`);
        const neighbors = await getJSON(
            `/api/brain/memory/semantic?q=${encodeURIComponent((m.content || "").slice(0, 300))}&limit=6`
        ).catch(() => ({ results: [] }));
        renderInspector(m, neighbors.results || []);
    } catch (e) {
        ins.innerHTML = `<div class="ins-header"><span class="ins-title">Record</span>
            <button class="ins-close" aria-label="Close">×</button></div>
            <div class="ins-body">${paneErrorHTML("Couldn't load this record: " + e.message)}</div>`;
        ins.querySelector(".ins-close").addEventListener("click", closeInspector);
        ins.querySelector(".pe-retry")?.addEventListener("click", () => openInspector(id));
    } finally { syncEnd(); }
}
function closeInspector() {
    state.selectedId = null;
    $("#mem-app").classList.remove("inspector-open");
    $("#inspector").setAttribute("aria-hidden", "true");
    $$("#lens-body .mem-row").forEach((el) => el.classList.remove("selected"));
    syncScrim();
}

function renderInspector(m, neighbors) {
    const ins = $("#inspector");
    const pill = STATUS_PILL[m.status] || "";
    const meta = m.metadata || {};

    // Validity lifeline — only if temporal fields present
    let lifeline = "";
    if (m.t_valid_from) {
        const live = !m.t_valid_until;
        const from = fmtDate(m.t_valid_from);
        const to = live ? "now" : fmtDate(m.t_valid_until);
        lifeline = `<div class="ins-field"><span class="ins-label">Validity</span>
            <div class="lifeline ${live ? "live" : ""}"><span style="width:100%"></span></div>
            <div class="lifeline-cap"><span>${esc(from)}</span><span class="${live ? "live-tail" : ""}">${esc(to)}</span></div></div>`;
    }

    // Resolution state (goal / unresolved)
    const isGoal = m.memory_type === "goal" || m.memory_type === "unresolved";
    const resolution = isGoal
        ? `<div class="ins-field"><span class="ins-label">Resolution</span>
            <div class="ins-value">${m.resolved_at
                ? `<span class="cseries-pill allow">resolved</span> <span class="mono">${esc(fmtDate(m.resolved_at))}</span>`
                : `<span class="cseries-pill warn">open</span> — mark it resolved below when it's done or answered.`}</div></div>`
        : "";

    // A rule, or a preference in a rule's own words (the server says which).
    const rule = m.rule_kind
        ? `<div class="ins-field"><span class="ins-label">Rule</span>
            <div class="ins-value">A ${esc(m.rule_kind)} rule. It rides in "Things You Were Taught", and nothing automatic retires it.</div></div>`
        : m.stored_skill
        ? `<div class="ins-field"><span class="ins-label">Stored skill</span>
            <div class="ins-value">A stored skill, not a rule you taught or your Host learned. It rides in "Things You Were Taught"; your Host does not change it. Archive or Delete it here to remove it.</div></div>`
        : (m.held_by_rule
            ? `<div class="ins-field"><span class="ins-label">Held by a rule</span>
                <div class="lin-row" data-id="${m.held_by_rule}"><span class="lin-dir">→</span><span class="lin-txt">Rule #${m.held_by_rule} says the same words. This copy is kept where that rule is not read, and never shows beside it.</span></div></div>`
            : "");

    // Lineage (clickable walk)
    const lin = [];
    if (m.correction_of) lin.push(`<div class="lin-row" data-id="${m.correction_of}"><span class="lin-dir up">↑</span><span class="lin-txt">Replaced #${m.correction_of} — what this superseded</span></div>`);
    if (m.corrected_by) lin.push(`<div class="lin-row" data-id="${m.corrected_by}"><span class="lin-dir">↓</span><span class="lin-txt">Superseded by #${m.corrected_by} — what replaced this</span></div>`);
    if (m.resolution_pointer && m.resolution_pointer !== m.corrected_by) lin.push(`<div class="lin-row" data-id="${m.resolution_pointer}"><span class="lin-dir">↓</span><span class="lin-txt">Resolves to #${m.resolution_pointer}</span></div>`);
    if (meta.supersedes_id && meta.supersedes_id !== m.correction_of) lin.push(`<div class="lin-row" data-id="${esc(meta.supersedes_id)}"><span class="lin-dir up">↑</span><span class="lin-txt">Replaced #${esc(meta.supersedes_id)} (consolidation)</span></div>`);
    const lineage = lin.length
        ? `<div class="ins-field"><span class="ins-label">Lineage</span>${lin.join("")}</div>`
        : "";

    // Provenance
    const provBits = [];
    if (meta.source || meta.original_source) provBits.push(`source: ${esc(meta.source || meta.original_source)}`);
    if (meta.title) provBits.push(`title: ${esc(meta.title)}`);
    // source_session is a per-REQUEST trace uuid in the sessionless
    // architecture (minted fresh for every message; nothing groups by it) —
    // label it honestly so it doesn't read as a conversation id.
    if (m.source_session) provBits.push(`request: ${esc(m.source_session)}`);
    if (m.source_message_idx != null) provBits.push(`msg #${m.source_message_idx}`);
    if (meta.promoted_from_context) provBits.push(`promoted from context #${meta.promoted_from_context}`);
    const provenance = provBits.length
        ? `<div class="ins-field"><span class="ins-label">Provenance</span><div class="ins-value mono">${provBits.join("  ·  ")}</div></div>`
        : "";

    // Strength bars
    const sb = (label, val) => {
        const pct = Math.round(Math.max(0, Math.min(1, val || 0)) * 100);
        return `<div class="strength-row"><span class="sl">${label}</span>
            <span class="cv-rate-bar" style="width:auto;flex:1"><span style="width:${pct}%"></span></span>
            <span class="sv">${pct}%</span></div>`;
    };
    const strength = `<div class="ins-field"><span class="ins-label">Strength</span>
        <div class="strength-grid">
            ${sb("Importance", m.importance)}
            ${sb("Confidence", m.confidence)}
            <div class="strength-row"><span class="sl">Recalled</span>
                <span class="cv-rate-bar" style="width:auto;flex:1"><span style="width:${Math.min(100, (m.access_count || 0) * 10)}%"></span></span>
                <span class="sv">${fmtNum(m.access_count)}×</span></div>
        </div></div>`;

    // Neighbors (similarity, explicitly NOT lineage)
    const nb = neighbors.filter((n) => n.id !== m.id).slice(0, 6);
    const neighborHTML = nb.length
        ? `<div class="ins-field"><span class="ins-label">Related entries</span>
            <div class="ins-value" style="color:var(--cseries-ash);margin-bottom:4px">Similarity query result — not lineage, not a stored edge.</div>
            ${nb.map((n) => `<div class="neighbor" data-id="${n.id}"><span class="nb-sim">${n.similarity != null ? (n.similarity * 100).toFixed(0) + "%" : ""}</span><span class="nb-txt">${esc((n.content || "").slice(0, 120))}</span></div>`).join("")}</div>`
        : "";

    const canCorrect = m.status === "active" && !m.t_valid_until;
    const canResolve = isGoal && !m.resolved_at;

    ins.innerHTML = `
        <div class="ins-header">
            <div>
                <div class="ins-title">${esc(m.memory_type || "record")}</div>
                <div class="ins-meta-row"><span class="cseries-pill ${pill}">${esc(m.status)}</span>
                    <span>#${m.id}</span></div>
            </div>
            <button class="ins-close" aria-label="Close">×</button>
        </div>
        <div class="ins-body">
            <div class="ins-field"><span class="ins-label">Content</span>
                <div class="ins-content">${esc(m.content)}</div></div>
            <div class="ins-edit" id="ins-edit" hidden>
                <textarea id="ins-edit-text" class="cseries-textarea"></textarea>
                <div class="ins-edit-row">
                    <button class="cseries-btn ghost" id="ins-edit-cancel">Cancel</button>
                    <button class="cseries-btn primary" id="ins-edit-save">Save as new version</button>
                </div>
                <div class="cv-card-sub">Corrections append — the record is closed and a new linked version takes its place. Nothing is overwritten.</div>
            </div>
            ${m.status_reason ? `<div class="ins-field"><span class="ins-label">Status reason</span><div class="ins-value mono" style="color:var(--cseries-clay)">${esc(m.status_reason)}</div></div>` : ""}
            ${resolution}
            ${rule}
            ${lifeline}
            ${lineage}
            ${provenance}
            ${strength}
            ${neighborHTML}
            <div class="ins-field"><span class="ins-label">Timestamps</span>
                <div class="ins-value mono">created ${esc(fmtDate(m.created_at))} · updated ${esc(relTime(m.updated_at))}${m.last_accessed_at ? " · last recalled " + esc(relTime(m.last_accessed_at)) : ""}</div></div>
        </div>
        <div class="ins-actions">
            ${canCorrect ? `<button class="cseries-btn" id="ins-correct">Correct</button>` : ""}
            ${canResolve ? `<button class="cseries-btn" id="ins-resolve">Mark resolved</button>` : ""}
            <button class="cseries-btn" id="ins-archive">Archive</button>
            <button class="cseries-btn danger" id="ins-delete">Delete</button>
        </div>`;

    ins.querySelector(".ins-close").addEventListener("click", closeInspector);
    $$("#inspector .lin-row, #inspector .neighbor").forEach((el) =>
        el.addEventListener("click", () => openInspector(Number(el.dataset.id))));
    $("#ins-archive").addEventListener("click", () => archiveMemory(m.id));
    $("#ins-delete").addEventListener("click", () => confirmDelete(m.id));
    const rc = $("#ins-resolve");
    if (rc) rc.addEventListener("click", () => resolveMemory(m.id));
    // Edit-form wiring happens ONCE here — wiring it inside the Correct
    // click handler stacked duplicate save listeners (double POST).
    $("#ins-edit-cancel").addEventListener("click", () => { $("#ins-edit").hidden = true; });
    $("#ins-edit-save").addEventListener("click", () => correctMemory(m.id));
    const cc = $("#ins-correct");
    if (cc) cc.addEventListener("click", () => {
        const box = $("#ins-edit");
        box.hidden = false;
        const ta = $("#ins-edit-text");
        ta.value = m.content || "";
        ta.focus();
    });
}

async function archiveMemory(id) {
    syncStart();
    try {
        await postJSON(`/api/brain/manage/memories/${id}/archive`, {});
        closeInspector(); loadFacets(); routeLens();
    } catch (e) { banner("error", "Archive failed: " + e.message); } finally { syncEnd(); }
}

async function resolveMemory(id) {
    syncStart();
    try {
        await postJSON(`/api/brain/manage/memories/${id}/resolve`, {});
        banner("success", `Marked #${id} resolved — it stops steering conversations and decays out naturally.`);
        loadFacets(); routeLens(); openInspector(id);
    } catch (e) { banner("error", "Resolve failed: " + e.message); } finally { syncEnd(); }
}

async function correctMemory(id) {
    const text = $("#ins-edit-text").value.trim();
    if (!text) { banner("error", "Enter the corrected content first."); return; }
    syncStart();
    try {
        const r = await postJSON(`/api/brain/manage/memories/${id}/edit`, { content: text });
        if (r.unchanged) { banner("info", "No change — the content is identical."); return; }
        banner("success", `Corrected — #${id} closed, #${r.new_id} is the live version.`);
        loadFacets(); routeLens(); openInspector(r.new_id);
    } catch (e) { banner("error", "Correction failed: " + e.message); } finally { syncEnd(); }
}

function confirmDelete(id) {
    const mount = $("#confirm-mount");
    mount.innerHTML = `<div class="confirm-overlay">
        <div class="confirm-box">
            <div class="confirm-title">Delete this memory?</div>
            <div class="confirm-message">This permanently removes record #${id}. Memory is append/correct — deletion can't be undone and breaks any lineage that points here.</div>
            <div class="confirm-actions">
                <button class="cseries-btn ghost" id="cd-cancel">Cancel</button>
                <button class="cseries-btn danger" id="cd-confirm">Delete</button>
            </div>
        </div></div>`;
    const close = () => { mount.innerHTML = ""; };
    $("#cd-cancel").addEventListener("click", close);
    mount.querySelector(".confirm-overlay").addEventListener("click", (e) => { if (e.target === e.currentTarget) close(); });
    $("#cd-confirm").addEventListener("click", async () => {
        close();
        syncStart();
        try {
            await postJSON(`/api/brain/manage/memories/${id}/delete`, {});
            closeInspector(); loadFacets(); routeLens();
        } catch (e) { banner("error", "Delete failed: " + e.message); } finally { syncEnd(); }
    });
}

/* ================================================================
 * Data — the Host's own database: what it keeps for you, each table's rows
 * (read-only), and the snapshots it takes before a change that can
 * remove data, each restorable. Read through /api/world (use:world),
 * on the Host's own database role, never as the superuser. Built by
 * the platform on purpose: your view of what your Host keeps must not
 * depend on your Host.
 * ================================================================ */
const DATA_PAGE = 100;
async function loadData() {
    const body = $("#lens-body");
    body.innerHTML = skeletonRows(4);
    syncStart();
    try {
        const [d, h] = await Promise.all([getJSON("/api/world/describe"), getJSON("/api/world/history")]);
        if (state.lens !== "data") return;
        if (d.status && d.status !== "ok") throw new Error(d.result || "unavailable");
        renderData(d.objects || [], (h && h.snapshots) || []);
    } catch (e) {
        paneError(body, "Couldn't read your Host's database: " + e.message, loadData);
    } finally { syncEnd(); }
}
function dataObjectHTML(o) {
    const rows = o.kind === "table" && o.rows_estimate != null ? " · about " + fmtNum(o.rows_estimate) + " rows" : "";
    const cols = (o.columns || []).map((c) =>
        `<span class="data-col" title="${esc(c.purpose || "")}">${esc(c.name)} <em>${esc(c.type)}</em></span>`).join("");
    const canOpen = o.kind === "table" || o.kind === "view" || o.kind === "materialized view";
    return `<div class="data-obj" data-name="${esc(o.name)}">
        <div class="data-obj-head"><span class="data-obj-name">${esc(o.name)}</span>
            <span class="label-sys">${esc(o.kind)}${rows}</span></div>
        <div class="data-obj-purpose">${esc(o.purpose || "No purpose given.")}</div>
        ${cols ? `<div class="data-obj-cols">${cols}</div>` : ""}
        ${canOpen ? `<button class="cseries-btn ghost data-open">Show rows</button>` : ""}
    </div>`;
}
function renderData(objects, snaps) {
    const body = $("#lens-body");
    const objs = objects.length ? objects.map(dataObjectHTML).join("")
        : emptyState("Nothing kept yet",
            "When your Host stores something with a shape of its own, the tables it makes show up here.");
    const snapRows = snaps.length ? snaps.map((s) => `<div class="data-snap">
            <span>${esc(new Date(s.taken).toLocaleString())}</span>
            <span class="label-sys">${esc(s.reason)}</span>
            <button class="cseries-btn ghost data-restore" data-id="${esc(s.snapshot)}">Restore</button></div>`).join("")
        : `<div class="label-sys">No snapshots yet. One is taken before any change that can remove data.</div>`;
    body.innerHTML = `<section class="data-objects">${objs}</section>
        <div id="data-rows"></div>
        <section class="data-snaps"><h2 class="data-h">Snapshots</h2>${snapRows}</section>`;
    $$("#lens-body .data-open").forEach((b) => b.addEventListener("click", () =>
        loadDataRows(b.closest(".data-obj").dataset.name, 0)));
    $$("#lens-body .data-restore").forEach((b) => b.addEventListener("click", () => confirmRestore(b.dataset.id)));
}
function dataCell(v) {
    return esc(v !== null && typeof v === "object" ? JSON.stringify(v) : v);
}
async function loadDataRows(name, offset) {
    const host = $("#data-rows");
    host.innerHTML = skeletonRows(3);
    syncStart();
    try {
        const r = await getJSON(`/api/world/rows?name=${encodeURIComponent(name)}&limit=${DATA_PAGE}&offset=${offset}`);
        if (r.status !== "ok") throw new Error(r.result || "unavailable");
        const cols = r.columns || [];
        const grid = cols.length ? `<div class="data-grid-wrap cseries-scroll"><table class="data-grid">
                <thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
                <tbody>${(r.rows || []).map((row) =>
                    `<tr>${cols.map((c) => `<td>${dataCell(row[c])}</td>`).join("")}</tr>`).join("")}</tbody>
            </table></div>` : emptyState("No rows", offset ? "No more rows." : "This table is empty.");
        host.innerHTML = `<section class="data-rows"><h2 class="data-h">${esc(r.name)}</h2>${grid}
            <div class="data-pager">${offset > 0 ? `<button class="cseries-btn ghost" id="data-prev">Previous</button>` : ""}
            ${r.more ? `<button class="cseries-btn ghost" id="data-next">Next</button>` : ""}</div></section>`;
        const prev = $("#data-prev"), next = $("#data-next");
        if (prev) prev.addEventListener("click", () => loadDataRows(name, Math.max(0, offset - DATA_PAGE)));
        if (next) next.addEventListener("click", () => loadDataRows(name, offset + DATA_PAGE));
    } catch (e) {
        paneError(host, "Couldn't read " + name + ": " + e.message, () => loadDataRows(name, offset));
    } finally { syncEnd(); }
}
function confirmRestore(id) {
    const mount = $("#confirm-mount");
    mount.innerHTML = `<div class="confirm-overlay">
        <div class="confirm-box">
            <div class="confirm-title">Put your Host's database back?</div>
            <div class="confirm-message">Everything your Host keeps goes back to how it was at this snapshot. Anything made after it is removed. A snapshot of how it is now is taken first, so you can undo this.</div>
            <div class="confirm-actions">
                <button class="cseries-btn ghost" id="cr-cancel">Cancel</button>
                <button class="cseries-btn" id="cr-confirm">Restore</button>
            </div>
        </div></div>`;
    const close = () => { mount.innerHTML = ""; };
    $("#cr-cancel").addEventListener("click", close);
    mount.querySelector(".confirm-overlay").addEventListener("click", (e) => { if (e.target === e.currentTarget) close(); });
    $("#cr-confirm").addEventListener("click", async () => {
        close();
        syncStart();
        try {
            const r = await postJSON("/api/world/restore", { snapshot: id });
            if (r.status !== "ok") throw new Error(r.result || "restore failed");
            banner("success", "Restored. To undo, restore the snapshot taken just now.");
            loadData();
        } catch (e) { banner("error", "Restore failed: " + e.message); } finally { syncEnd(); }
    });
}

/* ================================================================
 * States: skeleton / empty / error / banner
 * ================================================================ */
function skeletonRows(n) {
    let h = "";
    for (let i = 0; i < n; i++) h += `<div class="skeleton skeleton-row"></div>`;
    return `<div class="mem-loading">${h}</div>`;
}
function emptyState(title, msg) {
    return `<div class="cseries-empty"><span class="empty-title">${esc(title)}</span>${esc(msg)}</div>`;
}
function paneErrorHTML(msg) {
    return `<div class="pane-error">${esc(msg)}
        <button class="cseries-btn pe-retry">Retry</button></div>`;
}
function paneError(host, msg, retry) {
    host.innerHTML = paneErrorHTML(msg);
    const btn = host.querySelector(".pe-retry");
    if (btn && retry) btn.addEventListener("click", retry);
}
function banner(tone, msg) {
    const mount = $("#confirm-mount");
    const el = document.createElement("div");
    el.className = "status-banner " + tone;
    el.style.cssText = "position:fixed;bottom:16px;left:50%;transform:translateX(-50%);z-index:var(--z-toast);max-width:480px;box-shadow:var(--cseries-shadow-dropdown)";
    el.textContent = msg;
    mount.appendChild(el);
    setTimeout(() => el.remove(), 4200);
}
