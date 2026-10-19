/* Browser app — shell logic.
 *
 * Talks to the cseries-browser sidecar via the kernel proxy at
 * /system-apps/browser/* (kind=external → control API :8830, harness-
 * backed). The viewport is a canvas painted by viewport.js from the
 * sidecar's CDP screencast — one WebSocket on the SAME app mount
 * (/system-apps/browser/viewport/ws), which is also the wake signal
 * for the on-demand harness.
 *
 * Agents (0.5.0): "Create agent" turns the sidecar's recorder on
 * (./v1/record/*); "Save agent" reviews the steps, seals the sign-in
 * entries through the kernel's existing /api/browser/forms/save, and
 * PUTs the steps to /api/browser/agents/<name>. The Agents panel lists,
 * runs, answers and deletes them. No AI anywhere in it.
 *
 * No build step. No frameworks. Direct DOM + fetch.
 */
(function () {
    "use strict";

    const $ = (id) => document.getElementById(id);

    // ── Helpers ─────────────────────────────────────────────────
    const API = "."; // relative — /system-apps/browser/...

    async function apiGet(path) {
        const r = await fetch(`${API}/${path}`);
        if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
        return r.json();
    }
    async function apiPost(path, body) {
        const r = await fetch(`${API}/${path}`, {
            method: "POST",
            headers: body ? { "Content-Type": "application/json" } : {},
            body: body ? JSON.stringify(body) : null,
        });
        if (!r.ok) throw new Error(`${path} → HTTP ${r.status}: ${await r.text()}`);
        return r.json();
    }
    // Inline status surface (replaces blocking alert/prompt/confirm).
    function setHint(html) {
        $("hint-text").innerHTML = html;
    }
    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, (c) => (
            { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
        ));
    }

    // ── Projects (FS grant target) ─────────────────────────────
    async function loadProjects() {
        const sel = $("project");
        try {
            // We don't have a projects-list endpoint yet on the sidecar
            // (Ship 6 lands one that reads /var/lib/cseries/projects/).
            // For now, list directly via the polyfill grant API: ask the
            // sidecar for the current granted root, but read the parent.
            const grantRes = await apiGet("current-grant");
            // grant returns {granted, path?, name?} — we don't have a
            // projects-enumeration. Fall back to user-typed override.
            // Placeholder: keep "(none)" + an "Other path…" option.
            sel.innerHTML = '<option value="">(none — read-only)</option>'
                + '<option value="__custom__">Enter project path…</option>';
        } catch (e) {
            sel.disabled = true;
        }
    }

    // ── Buttons ────────────────────────────────────────────────
    // A browser. An address bar, nothing preset. (0.4.0 — the presets that
    // sat here pointed at one vendor's products and the "Connect …" flow
    // beside them stored that vendor's login; neither is this platform's
    // business.)
    function normalizeUrl(raw) {
        const s = String(raw || "").trim();
        if (!s) return "";
        if (/^[a-z][a-z0-9+.-]*:\/\//i.test(s)) return s;
        return "https://" + s;
    }

    function wireBar() {
        const urlBox = $("url");
        const project = $("project");
        const projectPath = $("project-path");

        urlBox.addEventListener("keydown", (e) => {
            if (e.key === "Enter") { e.preventDefault(); $("btn-open").click(); }
        });

        project.addEventListener("change", () => {
            projectPath.hidden = project.value !== "__custom__";
            if (!projectPath.hidden) projectPath.focus();
        });

        $("btn-open").addEventListener("click", async () => {
            const btnOpen = $("btn-open");
            const url = normalizeUrl(urlBox.value);
            if (!url) { urlBox.focus(); return; }
            const pVal = project.value;
            const typedPath = projectPath.value.trim();
            if (pVal === "__custom__" && !typedPath) { projectPath.focus(); return; }

            btnOpen.disabled = true;
            try {
                // Set the FS grant if a project is selected
                if (pVal === "__custom__") {
                    setHint("Granting project access…");
                    try {
                        await apiPost("grant-fs", { path: typedPath, name: typedPath.split("/").pop() });
                    } catch (e) {
                        setHint("Grant failed: " + escapeHtml(e.message));
                        return;
                    }
                } else {
                    // Revoke the FS grant on no-project (keeps polyfill calls failing safely).
                    try { await apiPost("revoke-fs"); } catch (_) {}
                }
                // Navigate the sidecar page
                setHint("Opening " + escapeHtml(url) + "…");
                try {
                    const r = await apiPost("navigate", { url });
                    const title = r.title ? escapeHtml(r.title) : "(untitled)";
                    const code = (r.status != null) ? ` · HTTP ${r.status}` : "";
                    setHint("Opened <b>" + title + "</b>" + code + " — " + escapeHtml(r.url || url));
                } catch (e) {
                    setHint("Navigate failed: " + escapeHtml(e.message));
                }
            } finally {
                btnOpen.disabled = false;
            }
        });

    }

    // ── Kernel routes ──────────────────────────────────────────
    //
    // The kernel routes, NOT the sidecar proxy: sealing is a vault write
    // and it happens in the entity container, so these go to /api/... with
    // the window's app token (the shell injects it), exactly like Keys.
    async function kernel(path, body, method) {
        const r = await fetch(path, {
            method: method || "POST",
            headers: body !== undefined ? { "Content-Type": "application/json" } : {},
            body: body !== undefined ? JSON.stringify(body || {}) : undefined,
        });
        const text = await r.text();
        let json = null;
        try { json = text ? JSON.parse(text) : null; } catch (e) { /* not json */ }
        if (!r.ok) {
            throw new Error((json && json.detail) || text || `HTTP ${r.status}`);
        }
        return json;
    }

    // ── "Save these entries" ───────────────────────────────────
    // Reading the form and sealing it are two calls on purpose — the user
    // sees the list before anything is stored.
    let capture = null;   // the live preview, until saved or cancelled

    function closeCapture() {
        capture = null;
        $("form-capture").hidden = true;
        $("fc-status").textContent = "";
    }

    function renderCapture(cap) {
        capture = cap;
        $("fc-host").textContent = cap.host || "this site";
        const box = $("fc-fields");
        box.innerHTML = "";
        (cap.fields || []).forEach((f, i) => {
            const id = "fc-f-" + i;
            const row = document.createElement("label");
            row.className = "fc-row";
            row.setAttribute("for", id);

            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.id = id;
            cb.checked = true;
            cb.dataset.key = f.key;

            const name = document.createElement("span");
            name.className = "fc-label";
            name.textContent = f.label || f.key;

            const val = document.createElement("span");
            val.className = "fc-value" + (f.secret ? " fc-secret" : "");
            // A password's value never left the sidecar — show its shape
            // only, so the user can still tell a filled box from an empty
            // one without the value being put back on screen.
            val.textContent = f.secret
                ? "password · " + "•".repeat(Math.min(f.length || 8, 12))
                : (f.value || (f.checked ? "✓ ticked" : ""));

            row.appendChild(cb);
            row.appendChild(name);
            row.appendChild(val);
            box.appendChild(row);
        });
        $("form-capture").hidden = false;
        $("fc-save").disabled = false;
    }

    function wireFormCapture() {
        const btn = $("btn-save-form");
        if (!btn) return;

        btn.addEventListener("click", async () => {
            btn.disabled = true;
            setHint("Reading what you have filled in…");
            try {
                const cap = await kernel("/api/browser/forms/capture");
                if (!cap || !(cap.fields || []).length) {
                    setHint("Nothing filled in on this page yet.");
                    return;
                }
                renderCapture(cap);
                setHint("Choose what to keep, then press <b>Save selected</b>.");
            } catch (e) {
                setHint("Could not read the form: " + escapeHtml(e.message));
            } finally {
                btn.disabled = false;
            }
        });

        $("fc-cancel").addEventListener("click", () => {
            // The capture expires in the sidecar on its own; cancelling
            // just drops our handle on it. Nothing was ever stored.
            closeCapture();
            setHint("Nothing was saved.");
        });

        $("fc-save").addEventListener("click", async () => {
            if (!capture) return;
            const keys = Array.from(
                $("fc-fields").querySelectorAll("input[type=checkbox]:checked")
            ).map((c) => c.dataset.key);
            if (!keys.length) {
                $("fc-status").textContent = "Nothing selected.";
                return;
            }
            $("fc-save").disabled = true;
            $("fc-status").textContent = "Sealing…";
            try {
                const res = await kernel("/api/browser/forms/save", {
                    capture_id: capture.capture_id,
                    keys: keys,
                });
                const n = (res.saved || []).length;
                const skipped = (res.skipped || []).length;
                closeCapture();
                setHint(
                    `Saved ${n} entr${n === 1 ? "y" : "ies"} for `
                    + `<b>${escapeHtml(res.host)}</b>`
                    + (skipped ? ` (${skipped} skipped)` : "")
                    + ". Next time these can be filled in for you."
                );
            } catch (e) {
                $("fc-status").textContent = "Save failed: " + e.message;
                $("fc-save").disabled = false;
            }
        });
    }

    // ── Agents: record ─────────────────────────────────────────
    //
    // Recording is a MODE the user turns on by name and off with Stop.
    // The pill shows it is on and how many steps have landed. Nothing the
    // user types is recorded (control/recorder.py); the values a sign-in
    // needs are read once at submit and sealed with their approval below.
    let recording = null;    // {name} while on
    let recPoll = null;

    function showRec(on, name, count) {
        $("rec-pill").hidden = !on;
        $("btn-record").hidden = on;
        $("btn-rec-stop").hidden = !on;
        $("btn-rec-discard").hidden = !on;
        $("btn-save-form").hidden = on;
        if (on) {
            $("rec-name").textContent = name;
            $("rec-count").textContent = String(count || 0);
        }
    }

    async function pollRecording() {
        try {
            const st = await apiGet("v1/record");
            if (!st.recording) {
                // expired or stopped elsewhere
                recording = null;
                showRec(false);
                clearInterval(recPoll); recPoll = null;
                return;
            }
            $("rec-count").textContent = String(st.step_count || 0);
        } catch (e) { /* the browser may be asleep between polls */ }
    }

    function stepLabel(step) {
        const kind = Object.keys(step)[0];
        const b = step[kind];
        switch (kind) {
            case "go": return ["go to", b];
            case "click": return ["click", (b.role ? b.role + " " : "") + JSON.stringify(b.name || "")];
            case "type": return ["type into", JSON.stringify(b.field) + (b.key ? " ← " + b.key + " (sealed)" : (b.literal !== undefined ? " ← " + JSON.stringify(b.literal) : ""))];
            case "secret": return ["password into", JSON.stringify(b.field) + " ← " + b.key + " (vault)"];
            case "press": return ["press", b];
            case "select": return ["choose", JSON.stringify(b.value) + " in " + JSON.stringify(b.field)];
            case "wait_for_load": return ["wait", "for the page to load"];
            case "wait_for": return ["wait for text", JSON.stringify(b)];
            case "pause_if": return ["pause & ask me", "if the page shows " + JSON.stringify(b.text) + " → " + JSON.stringify(b.field)];
            case "expect_download": return ["download", b.match + (b.save_as ? " → " + b.save_as : "") + (b.example ? "  (e.g. " + b.example + ")" : "")];
            case "switch_page": return ["switch to page", String(b.index)];
            case "scroll": return ["scroll", String(b) + "px"];
            case "wait": return ["wait", String(b) + "ms"];
            default: return [kind, JSON.stringify(b)];
        }
    }

    // The review state: steps (with removed flags), fields per host, captures.
    let review = null;

    function renderReview(rec) {
        review = {
            name: rec.name, site: rec.site, hosts: rec.hosts || [],
            steps: (rec.steps || []).map((s) => ({ step: s, removed: false })),
            fields: rec.fields || [],       // [{host, fields:[masked]}]
            captures: rec.captures || {},   // host -> capture_id
        };
        $("rv-name").textContent = rec.name;
        $("rv-site").textContent = rec.site || "(no site — open the site first)";
        renderSteps();
        const fb = $("rv-fields");
        fb.innerHTML = "";
        if (!review.fields.length) {
            fb.innerHTML = '<p class="sub">No sign-in entries were read (no form was submitted while recording).</p>';
        }
        review.fields.forEach((site) => {
            const h = document.createElement("div");
            h.className = "rv-host";
            h.textContent = site.host;
            fb.appendChild(h);
            (site.fields || []).forEach((f) => {
                const row = document.createElement("div");
                row.className = "rv-field";
                const label = document.createElement("span");
                label.textContent = f.label || f.key;
                const v = document.createElement("span");
                v.className = "v";
                v.textContent = f.secret
                    ? "password · " + "•".repeat(Math.min(f.length || 8, 12))
                    : (f.value || (f.checked ? "✓ ticked" : ""));
                const keep = document.createElement("label");
                keep.className = "keep";
                if (f.secret) {
                    keep.textContent = "sealed in Keys";
                } else {
                    const cb = document.createElement("input");
                    cb.type = "checkbox";
                    cb.dataset.host = site.host;
                    cb.dataset.key = f.key;
                    cb.dataset.value = f.value || "";
                    keep.appendChild(cb);
                    keep.appendChild(document.createTextNode("keep in the agent file (not sealed)"));
                }
                row.appendChild(label); row.appendChild(v); row.appendChild(keep);
                fb.appendChild(row);
            });
        });
        // default schedule: none; first-run default = tomorrow 06:00 local
        $("rv-recurrence").value = "";
        const t = new Date(); t.setDate(t.getDate() + 1); t.setHours(6, 0, 0, 0);
        $("rv-fire-at").value = new Date(t.getTime() - t.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
        $("rv-status").textContent = "";
        $("rec-save").hidden = false;
    }

    function renderSteps() {
        const ol = $("rv-steps");
        ol.innerHTML = "";
        review.steps.forEach((s, i) => {
            const li = document.createElement("li");
            if (s.removed) li.className = "removed";
            const [k, what] = stepLabel(s.step);
            const kind = document.createElement("span"); kind.className = "kind"; kind.textContent = k;
            const w = document.createElement("span"); w.className = "what"; w.textContent = what;
            const rm = document.createElement("button"); rm.type = "button"; rm.className = "rm ghost";
            rm.textContent = s.removed ? "restore" : "remove";
            rm.addEventListener("click", () => { s.removed = !s.removed; renderSteps(); });
            li.appendChild(kind); li.appendChild(w); li.appendChild(rm);
            ol.appendChild(li);
        });
    }

    function addPauseStep() {
        const text = window.prompt ? null : null; // no native dialogs; build inline
        const li = document.createElement("li");
        li.innerHTML = '<span class="kind">pause & ask me</span>'
            + '<span class="what"><input class="pf-text" placeholder="if the page shows… (e.g. Enter the code)" /> '
            + '<input class="pf-field" placeholder="type my answer into… (e.g. Code)" /></span>'
            + '<button type="button" class="rm ghost">add</button>';
        $("rv-steps").appendChild(li);
        li.querySelector("button").addEventListener("click", () => {
            const t = li.querySelector(".pf-text").value.trim();
            const f = li.querySelector(".pf-field").value.trim();
            if (!t || !f) return;
            review.steps.push({ step: { pause_if: { text: t, field: f, submit: true } }, removed: false });
            renderSteps();
        });
        void text;
    }

    function localToIso(v) {
        if (!v) return "";
        const d = new Date(v);
        return isNaN(d.getTime()) ? "" : d.toISOString();
    }

    async function saveReview() {
        if (!review) return;
        const st = $("rv-status");
        $("rv-save").disabled = true;
        try {
            if (!review.site) throw new Error("the recording has no site — open the site, then record");
            // 1. Seal the sign-in entries: passwords always; plain entries
            //    unless the user chose to keep them in the file.
            const keepLiteral = {};
            $("rv-fields").querySelectorAll("input[type=checkbox]:checked").forEach((cb) => {
                keepLiteral[cb.dataset.host + "|" + cb.dataset.key] = cb.dataset.value;
            });
            let sealed = 0;
            for (const site of review.fields) {
                const cid = review.captures[site.host];
                const keys = (site.fields || []).map((f) => f.key)
                    .filter((k) => !(site.host + "|" + k in keepLiteral));
                if (!cid || !keys.length) continue;
                st.textContent = "Sealing entries for " + site.host + "…";
                const res = await kernel("/api/browser/forms/save", { capture_id: cid, keys: keys });
                sealed += (res.saved || []).length;
            }
            // 2. Steps: a `type` whose entry was kept literal carries the value.
            const steps = review.steps.filter((s) => !s.removed).map((s) => {
                const step = JSON.parse(JSON.stringify(s.step));
                if (step.type) {
                    // Keyed on the host the entry was read under — the page
                    // the form was on, which the step carries — not the site.
                    const key = (step.type.host || review.site) + "|" + step.type.key;
                    delete step.type.secret;
                    if (key in keepLiteral) {
                        step.type.literal = keepLiteral[key];
                        delete step.type.key;
                        delete step.type.host;
                    }
                }
                if (step.expect_download) delete step.expect_download.example;
                return step;
            });
            if (!steps.length) throw new Error("no steps left — record something first");
            // 3. Schedule.
            const body = { site: review.site, allow: review.hosts.filter((h) => h !== review.site), steps: steps };
            const rec = $("rv-recurrence").value;
            const fireAt = localToIso($("rv-fire-at").value);
            if (rec) {
                if (!fireAt) throw new Error("pick when the first run should happen");
                body.schedule = { recurrence: rec, fire_at: fireAt };
            }
            st.textContent = "Saving agent…";
            const saved = await kernel("/api/browser/agents/" + encodeURIComponent(review.name), body, "PUT");
            $("rec-save").hidden = true;
            review = null;
            setHint(`Agent <b>${escapeHtml(saved.name)}</b> saved`
                + (sealed ? ` · ${sealed} sign-in entr${sealed === 1 ? "y" : "ies"} sealed in Keys` : "")
                + (saved.schedule ? ` · runs ${escapeHtml(saved.schedule.recurrence || "once")}` : "")
                + `. <b>Allow ${escapeHtml(saved.site)}</b> under Keys → Sign-ins before its first unattended run.`);
        } catch (e) {
            st.textContent = "Save failed: " + e.message;
        } finally {
            $("rv-save").disabled = false;
        }
    }

    function wireRecording() {
        $("btn-record").addEventListener("click", () => {
            $("rs-name").value = "";
            $("rs-status").textContent = "";
            $("rec-start").hidden = false;
            $("rs-name").focus();
        });
        $("rs-cancel").addEventListener("click", () => { $("rec-start").hidden = true; });
        $("rs-name").addEventListener("keydown", (e) => {
            if (e.key === "Enter") { e.preventDefault(); $("rs-go").click(); }
        });
        $("rs-go").addEventListener("click", async () => {
            const name = $("rs-name").value.trim().toLowerCase();
            if (!/^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$/.test(name)) {
                $("rs-status").textContent = "2-40 lowercase letters, digits and hyphens.";
                return;
            }
            $("rs-go").disabled = true;
            try {
                await apiPost("v1/record/start", { name });
                recording = { name };
                $("rec-start").hidden = true;
                showRec(true, name, 0);
                recPoll = setInterval(pollRecording, 2000);
                setHint("<b>Recording.</b> Do the task in the viewport exactly once — sign in, click through, download. Then press <b>Stop &amp; save</b>.");
                $("viewport").focus();
            } catch (e) {
                $("rs-status").textContent = "Could not start: " + e.message;
            } finally {
                $("rs-go").disabled = false;
            }
        });
        $("btn-rec-stop").addEventListener("click", async () => {
            if (!recording) return;
            $("btn-rec-stop").disabled = true;
            try {
                const rec = await apiPost("v1/record/stop", {});
                clearInterval(recPoll); recPoll = null;
                recording = null;
                showRec(false);
                renderReview(rec);
            } catch (e) {
                setHint("Could not stop the recording: " + escapeHtml(e.message));
            } finally {
                $("btn-rec-stop").disabled = false;
            }
        });
        $("btn-rec-discard").addEventListener("click", async () => {
            if (!recording) return;
            try { await apiPost("v1/record/stop", { discard: true }); } catch (_) {}
            clearInterval(recPoll); recPoll = null;
            recording = null;
            showRec(false);
            setHint("Recording discarded. Nothing was saved.");
        });
        $("rv-cancel").addEventListener("click", () => {
            // The captures expire in the sidecar on their own.
            $("rec-save").hidden = true;
            review = null;
            setHint("Agent not saved. Nothing was sealed.");
        });
        $("rv-add-pause").addEventListener("click", addPauseStep);
        $("rv-save").addEventListener("click", saveReview);
        $("rv-recurrence").addEventListener("change", () => {
            $("rv-fire-at").disabled = !$("rv-recurrence").value;
        });
    }

    // ── Agents: list / run / answer / delete ───────────────────
    function fmtRun(r) {
        if (!r) return ["never run", ""];
        const when = r.finished_at || r.started_at || "";
        const d = when ? new Date(when).toLocaleString() : "";
        if (r.status === "ok") return [`ok · ${d}` + (r.outputs && r.outputs.length ? ` · ${r.outputs.length} file${r.outputs.length === 1 ? "" : "s"}` : ""), "ok"];
        if (r.status === "failed") return [`failed · ${d} · ${r.error || ""}`, "failed"];
        if (r.status === "waiting") return [`waiting for you: ${r.question || ""}`, "waiting"];
        return [`${r.status} · step ${r.step}/${r.steps}`, ""];
    }

    async function renderAgents() {
        const box = $("ag-list");
        const st = $("ag-status");
        try {
            const res = await kernel("/api/browser/agents", undefined, "GET");
            const running = {};
            (res.running || []).forEach((r) => { running[r.agent] = r; });
            box.innerHTML = "";
            if (!(res.agents || []).length) {
                box.innerHTML = '<p class="sub">No agents yet. Press <b>Create agent</b>, do a task once, and save it.</p>';
                return;
            }
            res.agents.forEach((a) => {
                const row = document.createElement("div");
                row.className = "ag-row";
                const name = document.createElement("div");
                name.innerHTML = `<div class="name">${escapeHtml(a.name)}</div>`
                    + `<div class="meta">${escapeHtml(a.site || "")} · ${a.steps} steps`
                    + (a.schedule ? ` · ${escapeHtml(a.schedule.recurrence || "once")}` : "")
                    + (a.invalid ? ` · <span class="failed">file needs a fix: ${escapeHtml(a.invalid)}</span>` : "")
                    + `</div>`;
                const live = running[a.name] || a.last_run;
                const [txt, cls] = fmtRun(live);
                const last = document.createElement("div");
                last.className = "last " + cls;
                last.textContent = txt;
                last.title = txt;
                const acts = document.createElement("div");
                acts.className = "acts";
                const run = document.createElement("button"); run.type = "button"; run.textContent = a.running ? "Running…" : "Run now";
                run.disabled = !!a.running || !!a.invalid;
                run.addEventListener("click", async () => {
                    run.disabled = true; st.textContent = `Running ${a.name}…`;
                    try {
                        const r = await kernel("/api/browser/agents/" + encodeURIComponent(a.name) + "/run", { wait_s: 20 });
                        const [t] = fmtRun(r);
                        st.textContent = `${a.name}: ${t}`;
                    } catch (e) { st.textContent = `${a.name}: ${e.message}`; }
                    renderAgents();
                });
                const del = document.createElement("button"); del.type = "button"; del.className = "danger"; del.textContent = "Delete";
                del.addEventListener("click", async () => {
                    if (del.dataset.armed !== "1") { del.dataset.armed = "1"; del.textContent = "Really delete?"; return; }
                    try { await kernel("/api/browser/agents/" + encodeURIComponent(a.name), undefined, "DELETE"); }
                    catch (e) { st.textContent = e.message; }
                    renderAgents();
                });
                acts.appendChild(run); acts.appendChild(del);
                row.appendChild(name); row.appendChild(last); row.appendChild(acts);
                box.appendChild(row);
                if (live && live.status === "waiting") {
                    const ans = document.createElement("div");
                    ans.className = "ag-answer";
                    const inp = document.createElement("input"); inp.placeholder = "your answer (the code)"; inp.autocomplete = "off";
                    const go = document.createElement("button"); go.type = "button"; go.textContent = "Send";
                    go.addEventListener("click", async () => {
                        if (!inp.value.trim()) return;
                        try {
                            await kernel("/api/browser/agents/" + encodeURIComponent(a.name) + "/resume", { answer: inp.value.trim() });
                            st.textContent = `${a.name}: answer sent, the run continues.`;
                        } catch (e) { st.textContent = e.message; }
                        setTimeout(renderAgents, 1500);
                    });
                    ans.appendChild(inp); ans.appendChild(go);
                    box.appendChild(ans);
                }
            });
        } catch (e) {
            box.innerHTML = `<p class="sub">Could not list agents: ${escapeHtml(e.message)}</p>`;
        }
    }

    let agentsPoll = null;
    function wireAgents() {
        $("btn-agents").addEventListener("click", () => {
            $("ag-status").textContent = "";
            $("agents").hidden = false;
            renderAgents();
            agentsPoll = setInterval(renderAgents, 5000);
        });
        $("ag-close").addEventListener("click", () => {
            $("agents").hidden = true;
            clearInterval(agentsPoll); agentsPoll = null;
        });
    }

    // ── Boot ───────────────────────────────────────────────────
    document.addEventListener("DOMContentLoaded", async () => {
        wireBar();
        wireFormCapture();
        wireRecording();
        wireAgents();
        // Connect the live viewport immediately — opening it is what
        // wakes the on-demand browser harness, and the user sees
        // whatever the sidecar's persistent page is on.
        window.CSeriesViewport.init({
            canvas: $("viewport"),
            stage: $("stage"),
            pagebar: $("pagebar"),
            onStatus: (s) => { $("vp-status").textContent = s; },
        });
        await loadProjects();
        // A recording left on (the window was reloaded mid-task) is picked
        // back up rather than silently continuing unseen.
        try {
            const st = await apiGet("v1/record");
            if (st.recording) {
                recording = { name: st.name };
                showRec(true, st.name, st.step_count);
                recPoll = setInterval(pollRecording, 2000);
            }
        } catch (_) { /* browser asleep */ }
        $("url").focus();
    });
})();
