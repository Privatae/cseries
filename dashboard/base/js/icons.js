/**
 * C-Series — Icon Library (the ONE source of truth for app glyphs)
 *
 * Every surface that renders an app icon — desktop, dock, launcher grid,
 * window headers, taskbar, floating bubbles — calls CSeriesIcons.markup().
 * This file replaced three divergent copies (index.html <symbol> defs,
 * dock-menu.js ICON_PATHS, assets/icons.svg) that only covered 12 of the
 * platform's apps; everything else fell back to letter tiles.
 *
 * Glyph language: 24-grid outline icons (Lucide geometry — round caps,
 * 1.75 stroke), drawn in `currentColor` so the surrounding CSS owns the
 * silver ramp (rest / hover / active), with a single ember accent
 * (`--cseries-dusk`) reserved for the glyph's focal point. That accent is
 * the C-Series signature — at most one per glyph, none on purely utilitarian
 * marks.
 *
 * Load order: this script must precede dock-menu.js / app-runner.js in
 * index.html — both read window.iconMarkup at call time.
 */
const CSeriesIcons = (() => {
    const ACCENT = 'var(--cseries-dusk, #7fcfae)';

    // Glyph bodies keyed by app id. Rendered inside:
    //   <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ...>
    // Accent elements opt out with explicit fill/stroke attributes.
    const GLYPHS = {
        // ——— Brand marks ————————————————————————————————
        privatae: `<rect x="5" y="2.5" width="14" height="4" rx="1.5"/><rect x="5" y="10" width="14" height="4" rx="1.5" fill="${ACCENT}" stroke="none"/><rect x="5" y="17.5" width="14" height="4" rx="1.5"/>`,
        ceigas: `<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><circle cx="12" cy="12" r="2" fill="${ACCENT}" stroke="none"/>`,
        circles: `<circle cx="9" cy="12" r="5.5"/><circle cx="15" cy="12" r="5.5"/><circle cx="12" cy="12" r="1.5" fill="${ACCENT}" stroke="none"/>`,

        // ——— System ————————————————————————————————————
        settings: `<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>`,
        security: `<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><circle cx="12" cy="10.5" r="1.8"/><path d="M12 12.3V15"/>`,
        keys: `<path d="M2.586 17.414A2 2 0 0 0 2 18.828V21a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h1a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h.172a2 2 0 0 0 1.414-.586l.814-.814a6.5 6.5 0 1 0-4-4z"/><circle cx="16.5" cy="7.5" r="1" fill="${ACCENT}" stroke="none"/>`,
        backups: `<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/>`,
        monitor: `<path d="M22 12h-3l-3 8L9 4l-3 8H2"/><circle cx="12" cy="12" r="1.4" fill="${ACCENT}" stroke="none"/>`,
        compute: `<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M15 2v2M15 20v2M2 15h2M2 9h2M20 15h2M20 9h2M9 2v2M9 20v2"/>`,
        network: `<rect x="16" y="16" width="6" height="6" rx="1"/><rect x="2" y="16" width="6" height="6" rx="1"/><rect x="9" y="2" width="6" height="6" rx="1"/><path d="M5 16v-3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v3"/><path d="M12 12V8"/>`,
        // A sweep over two rings, lit at its centre — the machines the host
        // keeps watch over, and what each one has reported (the ledger).
        oversight: `<path d="M19.07 4.93A10 10 0 1 0 22 12"/><path d="M16.24 7.76A6 6 0 1 0 18 12"/><path d="m12 12 7-7"/><circle cx="12" cy="12" r="1.6" fill="${ACCENT}" stroke="none"/>`,
        // A hull under a mast and a lit flag — a ship, and the one approval
        // that sends it.
        ships: `<path d="M3 15h18l-3 5H6z"/><path d="M12 3v12"/><path d="M12 4l6 8h-6"/><circle cx="18" cy="17.5" r="1.4" fill="${ACCENT}" stroke="none"/>`,
        // A page on its two rings with one day lit — your events, and the
        // work your Host set for a day.
        calendar: `<rect x="3" y="4.5" width="18" height="16.5" rx="2"/><path d="M3 9.5h18"/><path d="M8 2.5v4M16 2.5v4"/><circle cx="15.5" cy="15.5" r="1.7" fill="${ACCENT}" stroke="none"/>`,
        connections: `<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>`,
        portal: `<circle cx="18" cy="5" r="2.5"/><circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="19" r="2.5"/><path d="m8.3 10.7 7.4-4.4M8.3 13.3l7.4 4.4"/>`,
        // Ascending steps toward one lit point — the tour rising through
        // the host's stack (the app's canvas motif, in glyph form).
        'getting-started': `<path d="M3 21h4.5v-4.5H12V12h4.5V7.5H21"/><circle cx="18.5" cy="4.5" r="1.5" fill="${ACCENT}" stroke="none"/>`,

        // ——— Work surfaces ——————————————————————————————
        workbench: `<path d="m15 12-8.373 8.373a1 1 0 1 1-3-3L12 9"/><path d="m18 15 4-4"/><path d="m21.5 11.5-1.914-1.914A2 2 0 0 1 19 8.172V7l-2.26-2.26a6 6 0 0 0-4.202-1.756L9 2.96l.92.82A6.18 6.18 0 0 1 12 8.4V10l2 2h1.172a2 2 0 0 1 1.414.586L18.5 14.5"/>`,
        orchestrator: `<rect x="3" y="3" width="8" height="8" rx="2"/><path d="M7 11v4a2 2 0 0 0 2 2h4"/><rect x="13" y="13" width="8" height="8" rx="2"/>`,
        projects: `<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/><path d="M8 10v4"/><path d="M12 10v2"/><path d="M16 10v6"/>`,
        artifacts: `<path d="M12 2 2 7l10 5 10-5-10-5z"/><path d="m2 12 10 5 10-5"/><path d="m2 17 10 5 10-5"/>`,
        designs: `<path d="M15.707 21.293a1 1 0 0 1-1.414 0l-1.586-1.586a1 1 0 0 1 0-1.414l5.586-5.586a1 1 0 0 1 1.414 0l1.586 1.586a1 1 0 0 1 0 1.414z"/><path d="m18 13-1.375-6.874a1 1 0 0 0-.746-.776L3.235 2.028a1 1 0 0 0-1.207 1.207L5.35 15.879a1 1 0 0 0 .776.746L13 18"/><path d="m2.3 2.3 7.286 7.286"/><circle cx="11" cy="11" r="2"/>`,
        skills: `<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>`,
        oracle: `<path d="M10 2v7.31L4.34 19.03A2 2 0 0 0 6.07 22h11.86a2 2 0 0 0 1.73-2.97L14 9.31V2"/><path d="M8.5 2h7"/><path d="M7 16h10"/><circle cx="12" cy="18" r="1.4" fill="${ACCENT}" stroke="none"/>`,
        tools: `<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>`,

        // ——— Knowledge ————————————————————————————————
        'context-manager': `<path d="M12 7v14"/><path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z"/>`,
        'training-memory': `<path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/><path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/>`,

        // ——— Code ——————————————————————————————————————
        codespace: `<rect x="3" y="3" width="18" height="18" rx="2"/><path d="m10 9-2.5 3L10 15"/><path d="m14 9 2.5 3L14 15"/>`,
        'claude-code': `<path d="m4 17 6-6-6-6"/><path d="M12 19h8"/>`,
        opencode: `<rect x="3" y="3" width="18" height="18" rx="2"/><path d="m7 11 2-2-2-2"/><path d="M11 13h4"/>`,
        // Abstract crescent, not a vendor mark — same rule the other harness
        // glyphs follow (a wrong logo next to a name is worse than none).
        kimi: `<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>`,
        // Abstract orbit (a dot on an arc), not Google's mark — same rule.
        antigravity: `<path d="M4 15a8 8 0 0 1 16 0"/><circle cx="12" cy="7" r="2"/><path d="M8 19h8"/>`,
        gitea: `<line x1="6" x2="6" y1="3" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>`,

        // ——— Publishing / community ————————————————————
        community: `<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>`,
        sites: `<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>`,
        publisher: `<path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"/><path d="m21.854 2.147-10.94 10.939"/>`,
        'app-store': `<path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><path d="M3 6h18"/><path d="M16 10a4 4 0 0 1-8 0"/>`,
        browser: `<circle cx="12" cy="12" r="10"/><path d="m16.24 7.76-2.12 6.36-6.36 2.12 2.12-6.36z"/><circle cx="12" cy="12" r="1" fill="${ACCENT}" stroke="none"/>`,
    };

    // Ids that render another app's glyph.
    const ALIASES = {
        'claude-code-harness': 'claude-code',
    };

    /**
     * Inline SVG for an app — self-contained, no <use>/defs resolution.
     * `app` is a manifest-shaped object ({ id, name, icon_url }) or a bare
     * id string. Unknown apps fall back to their uploaded icon_url, then
     * to a letter tile — same contract the old iconMarkup had.
     */
    function markup(app, size) {
        size = size || 34;
        const a = (typeof app === 'string') ? { id: app } : (app || {});
        const body = GLYPHS[a.id] || GLYPHS[ALIASES[a.id]];
        if (body) {
            return `<svg class="cseries-icon" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
        }
        if (a.icon_url) {
            return `<img src="${attr(a.icon_url)}" alt="" style="width:${size}px;height:${size}px;object-fit:contain;">`;
        }
        return `<span class="dock-tile-letter">${esc((a.name || a.id || '?').charAt(0).toUpperCase())}</span>`;
    }

    function has(appId) {
        return !!(GLYPHS[appId] || GLYPHS[ALIASES[appId]]);
    }

    // Escapes all five, so the result is safe as text and inside a quoted
    // attribute (the textContent→innerHTML trick left " and ' alone).
    function esc(s) {
        return String(s || '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
    function attr(s) {
        return String(s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    return { markup, has };
})();

window.CSeriesIcons = CSeriesIcons;
// Every icon consumer (app-runner, dock-menu, taskbar, floating-apps)
// resolves window.iconMarkup at call time.
window.iconMarkup = CSeriesIcons.markup;
