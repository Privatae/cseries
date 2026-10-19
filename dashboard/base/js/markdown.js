/**
 * markdown.js — shared escape-first markdown renderer.
 *
 * Extracted from the retired factory app's md() (the only renderer in the tree) in the
 * 2026-06-11 Circles upgrade so both chat surfaces render one way — same
 * DRY pass that produced chat-stream.js. Served from the kernel image at
 * /js/markdown.js via the dashboard overlay mount.
 *
 * Safety model: NO third-party sanitizer. Fenced code blocks are extracted
 * first, then ALL remaining input is HTML-escaped before any markup
 * substitution runs — so the only HTML in the output is what this file
 * emits. Keep it that way: never interpolate unescaped input.
 *
 * Usage:
 *   el.innerHTML = Markdown.render(rawText);
 *   Markdown.bindCopyButtons(el);   // once per container, event-delegated
 *
 * Supports: fenced + inline code, bold, italic, strikethrough, links,
 * autolinked bare URLs, h1-h4, blockquotes, ordered/unordered lists,
 * horizontal rules, paragraphs/line breaks.
 */

(function (root) {
    const Markdown = {};

    function esc(s) {
        return String(s == null ? "" : s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }
    Markdown.esc = esc;

    Markdown.render = function (raw) {
        if (!raw) return "";
        let text = raw;

        // Extract fenced code blocks first, replace with placeholders.
        const blocks = [];
        text = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
            if (lang === "artifact") {
                // Chat surfaces hydrate pre > code.lang-artifact into an
                // interactive panel (replacing the <pre>) — keep these
                // bare so no copy-header wrapper is left behind.
                blocks.push(`<pre><code class="lang-artifact">${esc(code.trimEnd())}</code></pre>`);
            } else {
                const langLabel = lang
                    ? `<span class="md-code-lang">${esc(lang)}</span>` : "";
                blocks.push(
                    `<div class="md-code-block">` +
                    `<div class="md-code-head">${langLabel}` +
                    `<button type="button" class="md-code-copy" title="Copy code">copy</button></div>` +
                    `<pre><code class="lang-${esc(lang)}">${esc(code.trimEnd())}</code></pre>` +
                    `</div>`
                );
            }
            return `%%CB${blocks.length - 1}%%`;
        });

        // Escape HTML in remaining text.
        text = esc(text);

        // Inline code (before other inline markup so its content is inert).
        text = text.replace(/`([^`]+)`/g, "<code>$1</code>");

        // Bold / italic / strikethrough.
        text = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
        text = text.replace(/(?<!\w)\*(.+?)\*(?!\w)/g, "<em>$1</em>");
        text = text.replace(/~~(.+?)~~/g, "<del>$1</del>");

        // [label](url) links — http(s) only.
        text = text.replace(
            /\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
            '<a href="$2" target="_blank" rel="noopener">$1</a>'
        );

        // Autolink bare URLs (skip ones already inside an href/tag).
        text = text.replace(
            /(^|[\s(])((?:https?:\/\/)[^\s<>"')]+)/gm,
            (m, pre, url) => `${pre}<a href="${url}" target="_blank" rel="noopener">${url}</a>`
        );

        // Headers.
        text = text.replace(/^#### (.+)$/gm, "<h4>$1</h4>");
        text = text.replace(/^### (.+)$/gm, "<h3>$1</h3>");
        text = text.replace(/^## (.+)$/gm, "<h2>$1</h2>");
        text = text.replace(/^# (.+)$/gm, "<h2>$1</h2>");

        // Horizontal rules.
        text = text.replace(/^(?:---+|\*\*\*+)$/gm, "<hr>");

        // Blockquotes (input is escaped, so > became &gt;).
        text = text.replace(/^&gt; (.+)$/gm, "<blockquote>$1</blockquote>");

        // Unordered lists (consecutive -/* lines).
        text = text.replace(
            /(^|\n)((?:[-*] .+\n?)+)/g,
            (_, pre, block) => {
                const items = block
                    .trim()
                    .split("\n")
                    .map((l) => `<li>${l.replace(/^[-*] /, "")}</li>`)
                    .join("");
                return `${pre}<ul>${items}</ul>`;
            }
        );

        // Ordered lists.
        text = text.replace(
            /(^|\n)((?:\d+\. .+\n?)+)/g,
            (_, pre, block) => {
                const items = block
                    .trim()
                    .split("\n")
                    .map((l) => `<li>${l.replace(/^\d+\. /, "")}</li>`)
                    .join("");
                return `${pre}<ol>${items}</ol>`;
            }
        );

        // Paragraphs: double newlines.
        text = text.replace(/\n\n+/g, "</p><p>");

        // Single newlines to <br> (but not adjacent to block tags).
        text = text.replace(/(?<!<\/?\w[^>]*)\n(?![<])/g, "<br>");

        // Restore code blocks LAST so neither the inline regexes nor the
        // paragraph/<br> pass can touch their contents. (The
        // original renderer restored them early — bold/italic markup inside a code
        // block got mangled; fixed in this extraction.)
        text = text.replace(/%%CB(\d+)%%/g, (_, i) => blocks[parseInt(i)]);

        return `<p>${text}</p>`.replace(/<p><\/p>/g, "");
    };

    /**
     * Wire copy buttons inside `container` via one delegated listener.
     * Safe to call multiple times — binds once per container.
     */
    Markdown.bindCopyButtons = function (container) {
        if (!container || container._mdCopyBound) return;
        container._mdCopyBound = true;
        container.addEventListener("click", (ev) => {
            const btn = ev.target.closest(".md-code-copy");
            if (!btn) return;
            const block = btn.closest(".md-code-block");
            const code = block && block.querySelector("code");
            if (!code) return;
            navigator.clipboard.writeText(code.textContent || "").then(() => {
                const orig = btn.textContent;
                btn.textContent = "copied ✓";
                setTimeout(() => { btn.textContent = orig; }, 1200);
            }).catch(() => {});
        });
    };

    root.Markdown = Markdown;
})(window);
