"use strict";
/* Compute — full cost-and-usage analytics.
 *
 * Three money lanes, never merged (each figure carries its lane):
 *   billed   — Privatae billables: what the platform actually charged.
 *   estimate — vendor/inference spend: the vendor bill is never visible to
 *              Privatae, so these are ESTIMATES, labeled everywhere.
 *   usage    — resources (seconds, tokens, bytes): metered, not money.
 *
 * SDK-managed app: the kernel-injected App SDK stamps the per-window token on
 * every fetch. No localStorage anywhere — all state lives in memory.
 */

const state = {
    window: "30d",
    view: "projects",
    runsFilter: { project_id: null, harness: null },
};

// ── formatting ───────────────────────────────────────────────────────────────
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
// NOT MEASURED IS NOT ZERO. Every formatter here used `Number(n) || 0`, so a
// null, an undefined or a missing field rendered as a confident "$0.00" or "0"
// — indistinguishable from a real measurement that happened to be zero. That is
// how this view came to assert things nobody ever measured: on one live Host,
// vendor_cost_microdollars is NULL on 100% of runs and rendered as "$0.00
// vendor-reported", and 58 of 73 harness runs carry no token count and rendered
// as "0 tokens".
//
// `—` is already the house style for it in this very file (the chat cache-hit
// card does `hitRate == null ? "—"`); it simply was not applied anywhere else.
const UNKNOWN = "—";
const isUnknown = (n) => n === null || n === undefined || n === ""
    || Number.isNaN(Number(n));
const fmtUsd = (n) => {
    if (isUnknown(n)) return UNKNOWN;
    n = Number(n);
    if (n !== 0 && Math.abs(n) < 0.01) return "$" + n.toFixed(4);
    return "$" + n.toFixed(2);
};
const fmtNum = (n) => isUnknown(n) ? UNKNOWN : Number(n).toLocaleString();
const fmtTokens = (n) => {
    if (isUnknown(n)) return UNKNOWN;
    n = Number(n);
    if (n < 1000) return String(n);
    if (n < 1_000_000) return (n / 1000).toFixed(1) + "k";
    return (n / 1_000_000).toFixed(2) + "M";
};
const fmtSecs = (s) => {
    if (isUnknown(s)) return UNKNOWN;
    s = Number(s);
    if (s < 90) return s.toFixed(0) + "s";
    if (s < 5400) return (s / 60).toFixed(1) + "m";
    return (s / 3600).toFixed(1) + "h";
};
const fmtPct = (f) => isUnknown(f) ? UNKNOWN : Math.round(100 * Number(f)) + "%";
const fmtDay = (d) => (d || "").slice(5); // MM-DD

const LANE_BADGE = {
    billed: `<span class="ca-badge ca-badge-billed" title="Privatae billable — actually charged">billed</span>`,
    est: `<span class="ca-badge ca-badge-est" title="Estimate — the vendor bill is never visible to Privatae; never billed by Privatae">estimate</span>`,
    usage: `<span class="ca-badge ca-badge-usage" title="Resource usage — metered, not money">usage</span>`,
};

// ── data layer: status-aware fetch (401/403/5xx must never look empty) ──────
async function getJSON(path, _retried) {
    let r;
    try {
        r = await fetch(path);
    } catch (e) {
        const err = new Error("network unreachable");
        err.kind = "network";
        throw err;
    }
    if (!r.ok) {
        let detail = "";
        try { detail = (await r.json()).detail || ""; } catch (e) { /* not json */ }
        if (r.status === 401 && !_retried) {
            // The App SDK token arrives over postMessage after first paint —
            // the opening fetch can beat it. One quiet retry before erroring.
            await new Promise(res => setTimeout(res, 1200));
            return getJSON(path, true);
        }
        const err = new Error(detail || ("HTTP " + r.status));
        err.status = r.status;
        err.kind = r.status === 401 ? "auth"
            : r.status === 403 ? "permission"
            : r.status === 503 ? "starting" : "server";
        throw err;
    }
    return r.json();
}

function load(endpoint, extraQS) {
    return getJSON(`/api/compute/${endpoint}?window=${state.window}${extraQS || ""}`);
}

function errorCard(e, retryFn) {
    const msg = {
        auth: "No app token yet — the desktop shell mints one automatically; retry in a moment.",
        permission: "This app's manifest doesn't grant the permission this panel needs.",
        starting: "The host is still starting (or its database isn't ready). This is not an empty dataset.",
        network: "Couldn't reach the host at all.",
        server: "The host returned an error.",
    }[e.kind] || "The host returned an error.";
    return `<div class="ca-error" role="alert">
        <div class="ca-error-title">Couldn't load — ${esc(e.message)}</div>
        <div class="ca-error-msg">${esc(msg)}</div>
        <button class="ca-retry" type="button" data-retry="${esc(retryFn)}">Retry</button>
    </div>`;
}

// ── charts (inline SVG; palette validated per the dataviz method) ───────────
// One series per chart wherever lanes differ — a chart never mixes billed
// money with estimates. Hover tooltip on every plot.
function chartSVG(points, opts) {
    // points: [{day, v}] (single series) or [{day, a, b}] for the stacked pair.
    const { color, color2, fmt, stacked, ariaLabel } = opts;
    if (!points.length) return `<div class="ca-chart-empty">No data in this window.</div>`;
    const W = 320, H = 132, padL = 40, padR = 6, padT = 8, padB = 18;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const maxV = Math.max(1e-9, ...points.map(p => stacked ? (p.a + p.b) : p.v));
    const n = points.length;
    const bw = Math.max(2, Math.min(18, (plotW / n) - 2));
    const x = (i) => padL + (n <= 1 ? plotW / 2 : (plotW * i) / (n - 1));
    const y = (v) => padT + plotH - (plotH * v) / maxV;
    const grid = [0, 0.5, 1].map(f => {
        const yy = padT + plotH - plotH * f;
        return `<line x1="${padL}" y1="${yy}" x2="${W - padR}" y2="${yy}" class="ca-grid"/>` +
            `<text x="${padL - 5}" y="${yy + 3}" class="ca-axis" text-anchor="end">${esc(fmt(maxV * f))}</text>`;
    }).join("");
    let marks = "";
    if (stacked) {
        marks = points.map((p, i) => {
            const xa = x(i) - bw / 2;
            const hA = plotH * p.a / maxV, hB = plotH * p.b / maxV;
            // 2px surface gap between stacked segments (dataviz mark spec).
            return `<rect x="${xa}" y="${y(p.a)}" width="${bw}" height="${Math.max(0, hA)}" rx="2" fill="${color}"/>` +
                (hB > 0 ? `<rect x="${xa}" y="${y(p.a + p.b) }" width="${bw}" height="${Math.max(0, hB - 2)}" rx="2" fill="${color2}"/>` : "");
        }).join("");
    } else if (opts.line) {
        const pts = points.map((p, i) => `${x(i)},${y(p.v)}`).join(" ");
        marks = `<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round"/>` +
            points.map((p, i) => `<circle cx="${x(i)}" cy="${y(p.v)}" r="2.5" fill="${color}"/>`).join("");
    } else {
        marks = points.map((p, i) =>
            `<rect x="${x(i) - bw / 2}" y="${y(p.v)}" width="${bw}" height="${Math.max(0, plotH - (y(p.v) - padT))}" rx="2" fill="${color}"/>`
        ).join("");
    }
    const tickIdx = n <= 3 ? points.map((_, i) => i) : [0, Math.floor(n / 2), n - 1];
    const xLabels = tickIdx.map(i =>
        `<text x="${x(i)}" y="${H - 4}" class="ca-axis" text-anchor="middle">${esc(fmtDay(points[i].day))}</text>`).join("");
    return `<svg viewBox="0 0 ${W} ${H}" class="ca-svg" role="img" aria-label="${esc(ariaLabel)}"
                 data-chart='${esc(JSON.stringify({ points, stacked: !!stacked }))}'
                 data-fmtkind="${esc(opts.fmtKind || "num")}">${grid}${marks}${xLabels}</svg>`;
}

// Shared hover tooltip: nearest-point lookup on any .ca-svg.
const tooltipEl = () => document.getElementById("ca-tooltip");
document.addEventListener("mousemove", (e) => {
    const svg = e.target.closest && e.target.closest(".ca-svg");
    const tip = tooltipEl();
    if (!svg || !svg.dataset.chart) { if (tip) tip.hidden = true; return; }
    let spec;
    try { spec = JSON.parse(svg.dataset.chart); } catch (err) { return; }
    const rect = svg.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (e.clientX - rect.left - rect.width * 0.12) / (rect.width * 0.85)));
    const i = Math.round(frac * (spec.points.length - 1));
    const p = spec.points[i];
    if (!p) { tip.hidden = true; return; }
    const fk = svg.dataset.fmtkind;
    const f = fk === "usd" ? fmtUsd : fk === "tok" ? fmtTokens : fmtNum;
    tip.innerHTML = spec.stacked
        ? `<strong>${esc(p.day)}</strong><br>${esc(p.la || "a")}: ${esc(f(p.a))}<br>${esc(p.lb || "b")}: ${esc(f(p.b))}`
        : `<strong>${esc(p.day)}</strong><br>${esc(f(p.v))}`;
    tip.style.left = (e.clientX + 12) + "px";
    tip.style.top = (e.clientY + 12) + "px";
    tip.hidden = false;
});

// ── views ───────────────────────────────────────────────────────────────────
const root = () => document.getElementById("view-root");

function unbilledWarn(n) {
    if (!n) return "";
    return ` <span class="ca-warn" title="${n} run${n === 1 ? "" : "s"} metered compute but the charge never reached the platform ledger (billing_ref missing) — an auditable gap, not a discount.">⚠ ${n} unbilled</span>`;
}

// The vendor estimate sums only the runs that reported a vendor cost
// (vendor_est_runs of runs). Fewer than all is a floor, never the group's
// vendor spend, so the cell says how many runs it covers, as tokens do.
function vendorTip(r) {
    if (isUnknown(r.vendor_est_usd)) return "estimate — no run here reported a vendor cost";
    return r.vendor_est_runs < r.runs
        ? `estimate — reported on ${r.vendor_est_runs} of ${r.runs} runs; the rest reported none, so the real total is higher`
        : "estimate — vendor-reported dollars, never billed by Privatae";
}
function vendorChip(r) {
    return !isUnknown(r.vendor_est_usd) && r.vendor_est_runs < r.runs
        ? ` <span class="ca-chip ca-chip-dim">${r.vendor_est_runs}/${r.runs}</span>` : "";
}

// -- Projects ---------------------------------------------------------------
async function renderProjects() {
    root().innerHTML = `<div class="ca-loading">Loading projects…</div>`;
    let data, ts;
    try {
        [data, ts] = await Promise.all([load("projects"), load("timeseries")]);
    } catch (e) {
        root().innerHTML = errorCard(e, "projects");
        return;
    }
    const ps = data.projects || [];
    const totBilled = ps.reduce((a, p) => a + p.billed_usd + p.est_compute_usd, 0);
    const totEst = ps.reduce((a, p) => a + p.est_compute_usd, 0);
    // Sum only the projects that actually reported a vendor cost. If NONE did
    // — the live case on one Host, where vendor_cost_microdollars is NULL on
    // every run — the total is null, not 0, so the card reads "—" instead of
    // claiming the vendor charged nothing.
    const vendorReported = ps.filter(p => !isUnknown(p.vendor_est_usd));
    const totVendor = vendorReported.length
        ? vendorReported.reduce((a, p) => a + Number(p.vendor_est_usd), 0) : null;
    const totRuns = ps.reduce((a, p) => a + p.runs, 0);
    // Runs the vendor total covers; the projects that reported none add 0.
    const totVendorRuns = ps.reduce((a, p) => a + (Number(p.vendor_est_runs) || 0), 0);
    const totAccepted = ps.reduce((a, p) => a + p.accepted, 0);
    const chatIn = ps.reduce((a, p) => a + (p.chat ? p.chat.tokens_in : 0), 0);
    const chatCached = ps.reduce((a, p) => a + (p.chat ? p.chat.tokens_cached : 0), 0);
    // tokens_in is the TRUE TOTAL context and tokens_cached is a SUBSET of it
    // (see _CHAT_LANE_FROM in server/routes/compute.py) — so the rate is
    // cached/in, and the two are never summed for a total.
    const hitRate = chatIn ? chatCached / chatIn : null;

    const hd = ts.harness_daily || [];
    const infD = ts.inference_daily || [];
    const chatD = ts.chat_daily || [];
    const estByDay = {};
    // A null vendor day contributes nothing rather than NaN-ing the series —
    // one unreported day would otherwise blank the whole chart.
    for (const r of hd) {
        if (isUnknown(r.vendor_est_usd)) continue;
        estByDay[r.day] = (estByDay[r.day] || 0) + Number(r.vendor_est_usd);
    }
    for (const r of infD) {
        if (isUnknown(r.est_cost_usd)) continue;
        estByDay[r.day] = (estByDay[r.day] || 0) + Number(r.est_cost_usd);
    }
    const estSeries = Object.keys(estByDay).sort().map(day => ({ day, v: estByDay[day] }));
    // The vendor share of each day sums only that day's runs that reported a
    // cost; when some did not, the chart says how many runs it covers.
    const dayRuns = hd.reduce((a, r) => a + (Number(r.runs) || 0), 0);
    const dayVendorRuns = hd.reduce((a, r) => a + (Number(r.vendor_est_runs) || 0), 0);
    const dayVendorNote = dayVendorRuns > 0 && dayVendorRuns < dayRuns
        ? `<div class="ca-fineprint">vendor share from ${fmtNum(dayVendorRuns)} of ${fmtNum(dayRuns)} runs — the rest reported none, so their days read low</div>`
        : "";

    root().innerHTML = `
      <section class="ca-cards">
        <div class="ca-card">
          <div class="ca-card-label">Privatae compute fee ${LANE_BADGE.billed}${totEst > 0 ? " " + LANE_BADGE.est : ""}</div>
          <div class="ca-card-value">${fmtUsd(totBilled)}</div>
          <div class="ca-card-sub">${totBilled === 0 && totRuns > 0 ? "usage metered — rate not set / charges pending"
            : totEst > 0 ? fmtUsd(totBilled - totEst) + " charged + " + fmtUsd(totEst) + " locally estimated"
            : "actually charged"}</div>
        </div>
        <div class="ca-card">
          <div class="ca-card-label">Vendor spend ${LANE_BADGE.est}</div>
          <div class="ca-card-value">${fmtUsd(totVendor)}</div>
          <div class="ca-card-sub">${totVendor === null
            ? "no harness reported a vendor cost in this window — not measured, not zero"
            : totVendorRuns < totRuns
            ? `vendor-reported on ${fmtNum(totVendorRuns)} of ${fmtNum(totRuns)} runs — the rest reported none, so the real total is higher; never billed by Privatae`
            : "vendor-reported; never billed by Privatae"}</div>
        </div>
        <div class="ca-card">
          <div class="ca-card-label">Harness runs ${LANE_BADGE.usage}</div>
          <div class="ca-card-value">${fmtNum(totRuns)}</div>
          <div class="ca-card-sub">${fmtNum(totAccepted)} accepted</div>
        </div>
        <div class="ca-card">
          <div class="ca-card-label">Chat cache hit ${LANE_BADGE.usage}</div>
          <div class="ca-card-value">${hitRate == null ? "—" : fmtPct(hitRate)}</div>
          <div class="ca-card-sub">${hitRate == null ? "no ledgered turns yet" : fmtTokens(chatCached) + " tokens served from cache"}</div>
        </div>
      </section>

      <section class="ca-chartrow">
        <div class="ca-chartbox">
          <div class="ca-chart-title">Privatae fee / day ${LANE_BADGE.billed}</div>
          ${chartSVG(hd.map(r => ({ day: r.day, v: r.billed_usd + r.est_compute_usd })), { color: "var(--viz-s1)", fmt: fmtUsd, fmtKind: "usd", ariaLabel: "Daily Privatae billed compute in dollars" })}
        </div>
        <div class="ca-chartbox">
          <div class="ca-chart-title">Vendor + inference / day ${LANE_BADGE.est}</div>
          ${chartSVG(estSeries, { color: "var(--viz-s2)", line: true, fmt: fmtUsd, fmtKind: "usd", ariaLabel: "Daily estimated vendor and inference spend in dollars" })}
          ${dayVendorNote}
        </div>
        <div class="ca-chartbox">
          <div class="ca-chart-title">Chat context / day ${LANE_BADGE.usage}</div>
          ${chartSVG(chatD.map(r => ({ day: r.day, a: r.tokens_cached, b: Math.max(0, r.tokens_in - r.tokens_cached), la: "cached", lb: "uncached" })), { color: "var(--viz-s3)", color2: "var(--viz-s1)", stacked: true, fmt: fmtTokens, fmtKind: "tok", ariaLabel: "Daily chat context tokens, cached versus uncached" })}
          <div class="ca-legend"><span><span class="ca-swatch" style="background:var(--viz-s3)"></span>cached</span><span><span class="ca-swatch" style="background:var(--viz-s1)"></span>uncached</span></div>
        </div>
      </section>

      <section class="ca-section">
        <div class="ca-section-head"><span class="ca-section-title">By project</span>
          <span class="ca-section-hint">click a row to drill into its runs</span></div>
        ${ps.length === 0 ? `<div class="ca-empty">No usage in this window. Run a harness task or chat with your host, then come back.</div>` : `
        <table class="ca-table">
          <thead><tr>
            <th>project</th><th class="num">runs</th><th class="num">accepted</th>
            <th class="num">privatae fee ${LANE_BADGE.billed}</th>
            <th class="num">vendor ${LANE_BADGE.est}</th>
            <th class="num">tokens</th><th class="num">compute</th>
            <th class="num">chat context</th><th class="num">cache&nbsp;hit</th>
          </tr></thead>
          <tbody>${ps.map(p => `
            <tr class="ca-row-link" data-project="${p.project_id == null ? "" : p.project_id}" tabindex="0">
              <td>${esc(p.project_name)}${unbilledWarn(p.unbilled_runs)}</td>
              <td class="num">${fmtNum(p.runs)}</td>
              <td class="num">${fmtNum(p.accepted)}</td>
              <td class="num">${fmtUsd(p.billed_usd + p.est_compute_usd)}${p.est_compute_usd > 0 ? `<span class="ca-fineprint" title="includes $${p.est_compute_usd} locally estimated (charge lane didn't price it)">*</span>` : ""}</td>
              <td class="num" title="${esc(vendorTip(p))}">${fmtUsd(p.vendor_est_usd)}${vendorChip(p)}</td>
              <td class="num" title="${p.tokens_covered_runs === p.runs
                    ? "every run in this project reported a token count"
                    : `counted from ${p.tokens_covered_runs} of ${p.runs} runs — the rest report no token count, so the real total is higher`}">${fmtTokens(p.tokens)}${
                    p.tokens_covered_runs < p.runs && p.tokens_covered_runs > 0
                        ? ` <span class="ca-chip ca-chip-dim">${p.tokens_covered_runs}/${p.runs}</span>` : ""}</td>
              <td class="num">${fmtSecs(p.compute_s)}</td>
              <td class="num">${p.chat ? fmtTokens(p.chat.tokens_in) : "—"}</td>
              <td class="num">${p.chat ? fmtPct(p.chat.cache_hit_rate) : "—"}</td>
            </tr>`).join("")}
          </tbody>
        </table>`}
      </section>`;
    root().querySelectorAll(".ca-row-link").forEach(tr => {
        const go = () => {
            const v = tr.dataset.project;
            state.runsFilter.project_id = v === "" ? null : Number(v);
            state.runsFilter.harness = null;
            switchView("runs");
        };
        tr.addEventListener("click", go);
        tr.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    });
}

// -- Billables --------------------------------------------------------------
async function renderBillables() {
    root().innerHTML = `<div class="ca-loading">Loading billables…</div>`;
    let data;
    try { data = await load("billables"); }
    catch (e) { root().innerHTML = errorCard(e, "billables"); return; }
    const rows = data.billables || [];
    const statusChip = (r) => {
        if (r.status === "live") return r.charged
            ? `<span class="ca-chip ca-chip-charged">charged</span>`
            : `<span class="ca-chip">visibility only</span>`;
        if (r.status === "not_exposed") return `<span class="ca-chip ca-chip-dim" title="${esc(r.note)}">metered platform-side — not visible here yet</span>`;
        if (r.status === "not_metered") return `<span class="ca-chip ca-chip-dim" title="${esc(r.note)}">not metered yet</span>`;
        if (r.status === "not_billed") return `<span class="ca-chip">never billed</span>`;
        return "";
    };
    root().innerHTML = `
      <section class="ca-section">
        <div class="ca-section-head"><span class="ca-section-title">Every Privatae billable and meter</span>
          <span class="ca-section-hint">what is charged, what is only measured, and what isn't measured at all — honestly</span></div>
        <div class="ca-billables">
        ${rows.map(r => `
          <div class="ca-billable">
            <div class="ca-billable-head">
              <span class="ca-billable-name">${esc(r.name)}</span>
              ${LANE_BADGE[r.lane === "billed" ? "billed" : r.lane === "estimate" ? "est" : "usage"]}
              ${statusChip(r)}
            </div>
            <div class="ca-billable-meter">${r.meter ? esc(r.meter) : "no meter exists for this yet"}</div>
            ${r.id === "harness_compute" ? `
              <div class="ca-billable-stats">
                <span><strong>${fmtUsd(r.billed_usd)}</strong> billed (${fmtNum(r.billed_runs)} run${r.billed_runs === 1 ? "" : "s"})</span>
                ${r.est_compute_usd > 0 ? `<span>${fmtUsd(r.est_compute_usd)} locally estimated ${LANE_BADGE.est}</span>` : ""}
                <span>${fmtSecs(r.compute_s)} metered</span>
                ${r.unpriced_runs > 0 ? `<span>${fmtNum(r.unpriced_runs)} run${r.unpriced_runs === 1 ? "" : "s"} captured unpriced (rate pending)</span>` : ""}
                ${r.unbilled_runs > 0 ? `<span class="ca-warn">⚠ ${fmtNum(r.unbilled_runs)} run${r.unbilled_runs === 1 ? "" : "s"} unbilled — metered compute whose charge never reached the ledger</span>` : ""}
              </div>` : ""}
            <div class="ca-billable-note">${esc(r.note || "")}</div>
          </div>`).join("")}
        </div>
      </section>`;
}

// -- Harnesses --------------------------------------------------------------
function harnessTable(rows, withModel) {
    if (!rows.length) return `<div class="ca-empty">No harness runs in this window.</div>`;
    // Built from the object rather than a hand-listed set of labels: listing
    // them is what dropped 'stream' and made codex read "sdk: 0, parsed: 0,
    // unknown: 9" against 11 runs. Zero-count sources are omitted so the tip
    // stays short, and the covered/total count leads because that is the
    // question the number below it raises.
    const srcTip = (r) => {
        const src = r.token_sources || {};
        const parts = Object.keys(src).filter(k => src[k] > 0)
            .map(k => `${k}: ${src[k]}`);
        const covered = r.runs - (src.unknown || 0);
        return `counted from ${covered} of ${r.runs} runs`
             + (parts.length ? ` — ${parts.join(", ")}` : "");
    };
    const cmTip = (r) => `cost methods — billed: ${r.cost_methods.billed}, estimated: ${r.cost_methods.estimated}, unpriced: ${r.cost_methods.unpriced}`;
    return `<table class="ca-table">
      <thead><tr>
        <th>harness</th>${withModel ? "<th>model</th>" : ""}
        <th class="num">runs</th><th class="num">ok</th><th class="num">accepted</th>
        <th class="num">tokens</th>
        <th class="num">wall p50 / p95</th>
        <th class="num">privatae fee ${LANE_BADGE.billed}</th>
        <th class="num">vendor ${LANE_BADGE.est}</th>
        <th class="num ca-hero">cost / accepted</th>
      </tr></thead>
      <tbody>${rows.map(r => `
        <tr>
          <td>${esc(r.harness)}${unbilledWarn(r.unbilled_runs)}</td>
          ${withModel ? `<td>${esc(r.model || "—")}</td>` : ""}
          <td class="num">${fmtNum(r.runs)}</td>
          <td class="num">${fmtNum(r.ok_runs)}</td>
          <td class="num">${fmtNum(r.accepted)} <span class="ca-fineprint">(${fmtPct(r.acceptance_rate)})</span></td>
          <td class="num" title="${esc(srcTip(r))}">${fmtTokens(r.tokens)}</td>
          <td class="num">${fmtSecs(r.med_wall_s)} / ${fmtSecs(r.p95_wall_s)}</td>
          <td class="num" title="${esc(cmTip(r))}">${fmtUsd(r.privatae_usd)}</td>
          <td class="num" title="${esc(vendorTip(r))}">${fmtUsd(r.vendor_est_usd)}${vendorChip(r)}</td>
          <td class="num ca-hero">${r.cost_per_accepted_usd == null ? "—" : fmtUsd(r.cost_per_accepted_usd)}</td>
        </tr>`).join("")}
      </tbody></table>`;
}

async function renderHarnesses() {
    root().innerHTML = `<div class="ca-loading">Loading harnesses…</div>`;
    let data;
    try { data = await load("harnesses"); }
    catch (e) { root().innerHTML = errorCard(e, "harnesses"); return; }
    root().innerHTML = `
      <section class="ca-section">
        <div class="ca-section-head"><span class="ca-section-title">By harness</span>
          <span class="ca-section-hint">cost per accepted run — acceptance is your decision (what you shipped), never a model's opinion</span></div>
        ${harnessTable(data.harnesses || [], false)}
      </section>
      <section class="ca-section">
        <div class="ca-section-head"><span class="ca-section-title">By harness and model</span></div>
        ${harnessTable(data.harness_models || [], true)}
      </section>
      <footer class="ca-methodology">${esc(data.methodology || "")}</footer>`;
}

// -- Runs -------------------------------------------------------------------
async function renderRuns() {
    root().innerHTML = `<div class="ca-loading">Loading runs…</div>`;
    const f = state.runsFilter;
    let qs = "";
    if (f.project_id != null) qs += `&project_id=${f.project_id}`;
    if (f.harness) qs += `&harness=${encodeURIComponent(f.harness)}`;
    let data, projects;
    try {
        [data, projects] = await Promise.all([load("runs", qs), load("projects")]);
    } catch (e) { root().innerHTML = errorCard(e, "runs"); return; }
    const runs = data.runs || [];
    const projOpts = (projects.projects || [])
        .filter(p => p.project_id != null)
        .map(p => `<option value="${p.project_id}" ${f.project_id === p.project_id ? "selected" : ""}>${esc(p.project_name)}</option>`).join("");
    const harnesses = [...new Set(runs.map(r => r.harness))];
    root().innerHTML = `
      <section class="ca-section">
        <div class="ca-section-head">
          <span class="ca-section-title">Runs</span>
          <span class="ca-filters">
            <label>project <select id="runs-project"><option value="">all</option>${projOpts}</select></label>
            <label>harness <input id="runs-harness" type="text" list="harness-list" value="${esc(f.harness || "")}" placeholder="all" size="10"></label>
            <datalist id="harness-list">${harnesses.map(h => `<option value="${esc(h)}">`).join("")}</datalist>
          </span>
        </div>
        ${runs.length === 0 ? `<div class="ca-empty">No runs match this window and filter.</div>` : `
        <table class="ca-table">
          <thead><tr>
            <th>when</th><th>project</th><th>harness</th><th>model</th><th>status</th>
            <th class="num">tokens</th><th class="num">compute</th>
            <th class="num">privatae fee ${LANE_BADGE.billed}</th>
            <th class="num">vendor ${LANE_BADGE.est}</th><th></th>
          </tr></thead>
          <tbody>${runs.map(r => `
            <tr class="ca-row-link" data-run="${r.id}" tabindex="0">
              <td>${esc(r.created_at.slice(0, 16).replace("T", " "))}</td>
              <td>${esc(r.project_name || "—")}</td>
              <td>${esc(r.harness)}</td>
              <td>${esc(r.model || "—")}</td>
              <td>${r.committed ? `<span class="ca-shipped">✓ shipped</span>` : r.ok ? "ok" : `<span class="ca-failed">failed</span>`}${r.unbilled ? unbilledWarn(1) : ""}</td>
              <td class="num" title="source: ${esc(r.token_source || "—")}">${fmtTokens(r.tokens)}</td>
              <td class="num">${fmtSecs(r.compute_s)}${r.vcpus ? ` <span class="ca-fineprint">×${esc(r.vcpus)}vcpu</span>` : ""}</td>
              <td class="num" title="cost method: ${esc(r.cost_method || "—")}">${fmtUsd(r.billed_usd + r.est_compute_usd)}</td>
              <td class="num" title="estimate — vendor-reported, never billed by Privatae">${fmtUsd(r.vendor_est_usd)}</td>
              <td class="ca-expand">▸</td>
            </tr>
            <tr class="ca-run-detail" data-detail="${r.id}" hidden><td colspan="10"><div class="ca-loading">Loading run…</div></td></tr>
          `).join("")}
          </tbody>
        </table>`}
      </section>`;
    document.getElementById("runs-project")?.addEventListener("change", (e) => {
        state.runsFilter.project_id = e.target.value === "" ? null : Number(e.target.value);
        renderRuns();
    });
    document.getElementById("runs-harness")?.addEventListener("change", (e) => {
        state.runsFilter.harness = e.target.value.trim() || null;
        renderRuns();
    });
    root().querySelectorAll("tr.ca-row-link[data-run]").forEach(tr => {
        const open = () => toggleRunDetail(Number(tr.dataset.run));
        tr.addEventListener("click", open);
        tr.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
    });
}

async function toggleRunDetail(runId) {
    const row = root().querySelector(`tr.ca-run-detail[data-detail="${runId}"]`);
    if (!row) return;
    if (!row.hidden) { row.hidden = true; return; }
    root().querySelectorAll("tr.ca-run-detail").forEach(r => { r.hidden = true; });
    row.hidden = false;
    let d;
    try { d = await getJSON(`/api/compute/runs/${runId}`); }
    catch (e) { row.firstElementChild.innerHTML = errorCard(e, "runs"); return; }
    const u = d.usage;
    row.firstElementChild.innerHTML = `
      <div class="ca-rundetail">
        <div class="ca-rundetail-grid">
          <span>session</span><span>${esc(d.session_id || "—")}</span>
          <span>exit code</span><span>${d.exit_code == null ? "—" : esc(d.exit_code)}</span>
          <span>diff</span><span>${fmtNum(d.diff_bytes)} bytes</span>
          <span>elapsed</span><span>${fmtSecs(d.elapsed_s)}</span>
          ${d.metadata && d.metadata.retries ? `<span>retries</span><span>${esc(d.metadata.retries)}</span>` : ""}
          ${u ? `
            <span>billing</span><span>${u.cost_method === "billed"
                ? `${fmtUsd(u.billed_usd)} billed (ref ${esc(u.billing_ref || "?")})`
                : u.cost_method === "compute"
                ? `${fmtUsd(u.est_compute_usd)} locally estimated — not charged`
                : "captured unpriced — rate pending"}${(d.compute_s > 0 && !u.billing_ref) ? ` <span class="ca-warn">⚠ charge never reached the ledger</span>` : ""}</span>
            <span>vendor</span><span title="estimate — never billed by Privatae">${
                isUnknown(u.vendor_est_usd)
                    ? `${UNKNOWN} <span class="ca-chip ca-chip-dim">not reported by the harness</span>`
                    : `${fmtUsd(u.vendor_est_usd)} (vendor-reported estimate)`
            }</span>` : ""}
        </div>
        ${d.error ? `<div class="ca-runerror">${esc(d.error)}</div>` : ""}
        ${d.transcript_stripped
            ? `<div class="ca-fineprint">Transcript archived by curation (run not shipped) — metrics retained.</div>`
            : d.transcript_tail
            ? `<details><summary>transcript tail (${fmtNum(d.transcript_chars)} chars total)</summary><pre class="ca-transcript">${esc(d.transcript_tail)}</pre></details>`
            : `<div class="ca-fineprint">No transcript captured.</div>`}
      </div>`;
}

// -- Inference (estimate lane) ----------------------------------------------
async function renderInference() {
    root().innerHTML = `<div class="ca-loading">Loading inference estimates…</div>`;
    let data;
    try { data = await load("inference"); }
    catch (e) { root().innerHTML = errorCard(e, "inference"); return; }
    const mods = data.by_module || [], models = data.by_model || [], chat = data.chat_by_model || [];
    const cov = data.price_coverage || {};
    const totalEst = mods.reduce((a, r) => a + (r.est_cost_usd || 0), 0);
    // A row's cost cell says WHICH kind of number it is. "$0.00" was being
    // shown for three different facts: priced-and-cheap, flat-plan (genuinely
    // no marginal cost), and no-rate-card-at-all. The last is the common case
    // and the least like zero.
    const costCell = (r) => {
        const priced = Number(r.priced_calls || 0);
        const unpriced = Number(r.unpriced_calls || 0);
        const sub = Number(r.subscription_calls || 0);
        if (priced > 0) {
            const partial = (unpriced + sub) > 0
                ? ` <span class="ca-chip ca-chip-dim" title="${unpriced} of ${priced + unpriced + sub} calls have no rate card; this figure covers the rest">partial</span>`
                : "";
            return `<td class="num" title="estimate — computed from token counts; the vendor bill is never visible">${fmtUsd(r.est_cost_usd)}${partial}</td>`;
        }
        if (sub > 0 && unpriced === 0) {
            return `<td class="num"><span class="ca-chip" title="covered by a flat plan — no per-token cost to estimate">flat plan</span></td>`;
        }
        return `<td class="num"><span class="ca-chip ca-chip-dim" title="no published rate card for this model, so no dollar figure can be honestly given — the tokens are real">unpriced</span></td>`;
    };
    const tokTable = (rows, nameKey, nameLabel) => rows.length === 0
        ? `<div class="ca-empty">No calls recorded in this window.</div>`
        : `<table class="ca-table"><thead><tr>
             <th>${nameLabel}</th><th class="num">calls</th><th class="num">tokens in</th>
             <th class="num">from cache</th><th class="num">cache writes</th>
             <th class="num">tokens out</th><th class="num">est. cost ${LANE_BADGE.est}</th>
           </tr></thead><tbody>${rows.map(r => `
           <tr><td>${esc(r[nameKey] || UNKNOWN)}</td><td class="num">${fmtNum(r.calls)}</td>
               <td class="num">${fmtTokens(r.tokens_in)}</td>
               <td class="num" title="a SUBSET of tokens in, priced at the provider's published cache-read rate — never added to it">${fmtTokens(r.tokens_cached)}</td>
               <td class="num" title="also a subset of tokens in: writing the cache costs MORE than fresh input (typically 1.25x) — it is what every later cheap hit is paid for up front">${fmtTokens(r.tokens_cache_write)}</td>
               <td class="num">${fmtTokens(r.tokens_out)}</td>
               ${costCell(r)}</tr>`).join("")}
           </tbody></table>`;
    root().innerHTML = `
      <div class="ca-banner">${LANE_BADGE.est} ${esc(data.disclaimer || "Estimates only — the vendor bill is never visible to Privatae.")}</div>
      <section class="ca-cards">
        <div class="ca-card"><div class="ca-card-label">Estimated inference ${LANE_BADGE.est}</div>
          <div class="ca-card-value">${fmtUsd(totalEst)}</div>
          <div class="ca-card-sub">${cov.total_calls
            ? `priced from ${fmtNum(cov.priced_calls)} of ${fmtNum(cov.total_calls)} calls`
              + (cov.unpriced_calls
                  ? ` — ${fmtNum(cov.unpriced_calls)} have no rate card (${fmtTokens(cov.unpriced_tokens)} tokens), so real spend is higher`
                  : "")
            : "this window, container-local log"}</div></div>
      </section>
      <section class="ca-section">
        <div class="ca-section-head"><span class="ca-section-title">Background calls by module</span></div>
        ${tokTable(mods, "source_module", "module")}
      </section>
      <section class="ca-section">
        <div class="ca-section-head"><span class="ca-section-title">Background calls by model</span></div>
        ${tokTable(models, "model_id", "model")}
      </section>
      <section class="ca-section">
        <div class="ca-section-head"><span class="ca-section-title">Chat turns by model ${LANE_BADGE.usage}</span>
          <span class="ca-section-hint">usage only — no dollars are invented for your own providers; cache hits are why a big-context turn can be cheap</span></div>
        ${chat.length === 0 ? `<div class="ca-empty">No chat turns with a context ledger in this window.</div>` : `
        <table class="ca-table"><thead><tr>
          <th>model</th><th class="num">turns</th><th class="num">calls</th>
          <th class="num">context in</th><th class="num">from cache</th>
          <th class="num">out</th><th class="num ca-hero">cache hit</th>
        </tr></thead><tbody>${chat.map(r => `
          <tr><td>${esc(r.model)}</td><td class="num">${fmtNum(r.turns)}</td>
              <td class="num">${fmtNum(r.calls)}</td>
              <td class="num">${fmtTokens(r.tokens_in)}</td>
              <td class="num">${fmtTokens(r.tokens_cached)}</td>
              <td class="num">${fmtTokens(r.tokens_out)}</td>
              <td class="num ca-hero">${fmtPct(r.cache_hit_rate)}</td></tr>`).join("")}
        </tbody></table>`}
      </section>`;
}

// ── shell wiring ────────────────────────────────────────────────────────────
const VIEWS = {
    projects: renderProjects,
    billables: renderBillables,
    harnesses: renderHarnesses,
    runs: renderRuns,
    inference: renderInference,
};

function switchView(view) {
    state.view = view;
    document.querySelectorAll(".ca-tab").forEach(t => {
        const on = t.dataset.view === view;
        t.classList.toggle("active", on);
        t.setAttribute("aria-selected", String(on));
    });
    refresh();
}

async function refresh() {
    await (VIEWS[state.view] || renderProjects)();
    const upd = document.getElementById("last-update");
    if (upd) upd.textContent = "updated " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

document.querySelector(".ca-tabs").addEventListener("click", (e) => {
    const b = e.target.closest(".ca-tab");
    if (b) switchView(b.dataset.view);
});
document.getElementById("window-picker").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    state.window = b.dataset.window;
    document.querySelectorAll("#window-picker button").forEach(x => {
        const on = x === b;
        x.classList.toggle("active", on);
        x.setAttribute("aria-pressed", String(on));
    });
    refresh();
});
// Retry buttons are injected into error cards; delegate.
document.addEventListener("click", (e) => {
    const b = e.target.closest(".ca-retry");
    if (b) refresh();
});

refresh();
setInterval(() => { if (document.visibilityState === "visible") refresh(); }, 60_000);
document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
});
