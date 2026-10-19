// Dark or light, read from the desktop's colours (book.css takes every colour
// from the desktop's tokens). The colour reading is pure, so `node --test`
// pins it (workbench-web/test/book.test.ts, through lib/book.ts);
// followScheme touches the document only when called.

/** The WCAG relative luminance of a colour as the browser computes it
 * ("rgb(14, 20, 22)", "rgba(…)", "color(srgb 0.05 0.08 0.09)"), or null when
 * it cannot be read. */
export function luminanceOf(css: string | null | undefined): number | null {
  const s = String(css ?? "").trim().toLowerCase();
  let rgb: number[] | null = null;
  const m = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/.exec(s);
  if (m) rgb = [m[1], m[2], m[3]].map((v) => Number(v) / 255);
  const c = /^color\(\s*srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)/.exec(s);
  if (!rgb && c) rgb = [c[1], c[2], c[3]].map(Number);
  if (!rgb || rgb.some((v) => !Number.isFinite(v))) return null;
  const lin = rgb.map((v) => {
    const x = Math.min(1, Math.max(0, v));
    return x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
}

/** A computed colour's alpha, 0 to 1 ("rgba(0, 0, 0, 0)", "… / 0.5)"); 1 when
 * it names none. */
export function alphaOf(css: string | null | undefined): number {
  const s = String(css ?? "").trim().toLowerCase();
  const m =
    /^rgba\(\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*,\s*([\d.]+)(%?)\s*\)$/.exec(s) ??
    /\/\s*([\d.]+)(%?)\s*\)$/.exec(s);
  if (!m) return 1;
  const a = Number(m[1]) / (m[2] ? 100 : 1);
  return Number.isFinite(a) ? Math.min(1, Math.max(0, a)) : 1;
}

/** Whether a surface is dark: light words on it contrast more than dark ones
 * (relative luminance under 0.179, where the two are equal). Null when the
 * colour cannot be read, and when it is drawn wholly clear: what shows there
 * is whatever lies under it. */
export function isDarkSurface(css: string | null | undefined): boolean | null {
  if (alphaOf(css) === 0) return null;
  const l = luminanceOf(css);
  return l == null ? null : l < 0.179;
}

/**
 * Dark or light is the desktop design's (book.css takes every colour from its
 * tokens): the page's colour, as this frame has it, decides the color-scheme
 * the browser draws its own controls in, and the `dark` class the shared
 * components' dark variants and the code blocks' dark theme (streamdown's
 * shiki-dark) hang on. Read again whenever the tokens can have changed: the
 * shell sets a design's tokens on this frame's root as it changes
 * (dashboard/base/js/desktop-surface.js applyTokens) and swaps in a fresh
 * /css/tokens.css (refreshTokenSheets).
 */
export function followScheme(doc: Document = document): () => void {
  const root = doc.documentElement;
  const probe = doc.createElement("span");
  probe.setAttribute("aria-hidden", "true");
  probe.style.cssText = "position:absolute;width:0;height:0;overflow:hidden;pointer-events:none";
  // A surface's colour, read as a background, which is never inherited: a
  // design's value the browser cannot draw (a digit short, "#0e141") leaves
  // it clear. Read as `color`, the probe would inherit the page's ink, the
  // opposite answer.
  const surface = (v: string) => {
    probe.style.backgroundColor = v;
    return getComputedStyle(probe).backgroundColor;
  };
  const read = () => {
    if (!probe.isConnected) (doc.body ?? root).appendChild(probe);
    // The page's colour; when that is drawn clear, the desk under it shows
    // (book.css: the sheet is var(--paper) on var(--desk)).
    const dark = isDarkSurface(surface("var(--paper)")) ?? isDarkSurface(surface("var(--desk)"));
    if (dark == null) return;
    if (root.classList.contains("dark") !== dark) root.classList.toggle("dark", dark);
    // Written only when it changes: the write is itself a change to the
    // attribute watched below.
    const scheme = dark ? "dark" : "light";
    if (root.style.colorScheme !== scheme) root.style.colorScheme = scheme;
  };
  read();
  // The tokens on the root: the shell's applyTokens.
  const onRoot = new MutationObserver(read);
  onRoot.observe(root, { attributes: true, attributeFilter: ["style"] });
  // A fresh tokens.css: read once it has loaded, and when the old one goes.
  const onHead = new MutationObserver((records) => {
    for (const r of records) {
      r.addedNodes.forEach((n) => {
        if (n instanceof HTMLLinkElement) n.addEventListener("load", read, { once: true });
      });
    }
    read();
  });
  if (doc.head) onHead.observe(doc.head, { childList: true });
  return () => {
    onRoot.disconnect();
    onHead.disconnect();
    probe.remove();
  };
}
