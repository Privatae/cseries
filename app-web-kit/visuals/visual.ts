// VISUALS — a page the Host draws right in its reply, the ```visual fence
// (the Host can reply with HTML when that suits the answer; the noun is
// "visual", the term Anthropic uses for the same split). An ARTIFACT is what the user keeps and iterates; a visual is part of
// the answer and is gone unless the user saves it. Shared by Circles and the
// Workbench.
//
// The fence's body is drawn in the same frame every host-authored document
// gets: sandbox="allow-scripts" (opaque origin, no session, no API) under the
// shared CSP from /js/sandbox-csp.js, failing CLOSED when that file did not
// load. Nothing here is saved anywhere: the block lives in the reply text, and
// keeping it is the user's click (Save as artifact / Download), never ours.
//
// Pure functions only, so node --test can exercise them without a DOM.

/** Does this turn's ```visual RUN, and do its artifact chips name
 * artifacts in this user's store? Only for the user's own Host, which only
 * the place that built the message can know (ChatMessage.ownHost). Fails
 * closed: a message nobody marked is somebody else's. */
export function isOwnHostTurn(m: { role: string; ownHost?: boolean }): boolean {
  return m.role === "assistant" && m.ownHost === true;
}

/** The fence language that renders. Plain ```html stays a code listing, so a
 * Host showing someone the SOURCE of a page is never turned into the page. */
export const VISUAL_LANG = "visual";

/** The one message a visual's frame may send: its content height. Anything
 * else from the frame is ignored. */
export const SIZE_MSG = "cseries:visual-size";

/** Frame height bounds, px. The floor keeps an empty or still-loading block
 * visible; past the ceiling the frame scrolls inside itself so one tall
 * reply cannot push the conversation off screen. Full size shows all of it. */
export const MIN_FRAME_H = 48;
export const MAX_FRAME_H = 720;

export function clampHeight(h: unknown): number | null {
  if (typeof h !== "number" || !Number.isFinite(h)) return null;
  return Math.min(MAX_FRAME_H, Math.max(MIN_FRAME_H, Math.ceil(h)));
}

/** The viewer-authored script the frame runs first (after the CSP, so the
 * policy already applies to it): report the document height on load and on
 * every resize. It reads nothing but its own layout and posts one number. */
export const SIZE_SCRIPT =
  "<scr" + "ipt data-visual-size>(function(){" +
  'function s(){try{var d=document.documentElement;parent.postMessage({type:"' + SIZE_MSG + '",h:Math.max(d.scrollHeight,document.body?document.body.scrollHeight:0)},"*")}catch(_){}}' +
  'addEventListener("DOMContentLoaded",s);addEventListener("load",s);' +
  'try{new ResizeObserver(s).observe(document.documentElement)}catch(_){}' +
  "})();</scr" + "ipt>";

/** Does the document declare itself a desktop design? Same test as the
 * Artifacts window and the Workbench version window. */
export function isDesktopDoc(html: string): boolean {
  return /<meta[^>]+name\s*=\s*["']cseries-desktop["']/i.test(String(html).slice(0, 65536));
}

// The desktop's look, as an artifact wears it when the Artifacts window shows
// it — a port of server/applications/artifact_style.py style_block(), so an
// inline reply and an artifact written with the same var(--cseries-*) names look
// the same. The sheet is /css/tokens.css, which every app already loads.
const BASE_CSS = `
:where(html) { background: var(--cseries-ink); color: var(--cseries-paper);
  font-family: var(--font-body, var(--cseries-font-body, system-ui, sans-serif)); }
:where(body) { margin: 0; }
:where(a) { color: var(--cseries-dusk); }
`;

export function tokenStyleBlock(sheet: string): string {
  if (!sheet || !sheet.trim()) return "";
  // @font-face points at the shell's /fonts/, which the sandbox cannot fetch.
  let css = sheet.replace(/@font-face\s*\{[^}]*\}/gi, "");
  // A literal </style> in the sheet would end the element early.
  css = css.replace(/<\/(style)/gi, "<\\/$1");
  return `<style data-cseries-artifact-style>\n${css}\n${BASE_CSS}</style>`;
}

export interface Sandbox {
  withSandboxCsp: (html: string, extra?: string) => string;
  externalRefs: (html: string) => string[];
}

/** The document as the frame mounts it, or null when it must not be shown.
 * Sealed first and always; no policy, no render. The desktop's style block
 * and the size reporter go AFTER the CSP and BEFORE the page's own markup, so
 * the policy covers them and every rule the page declares wins over the
 * defaults. A desktop design keeps its own tokens (artifact_style.py does the
 * same). */
export function frameDoc(
  html: string,
  sandbox: Sandbox | undefined,
  tokenSheet = "",
): { doc: string; blocked: string[] } | null {
  if (!sandbox) return null;
  const style = isDesktopDoc(html) ? "" : tokenStyleBlock(tokenSheet);
  return {
    doc: sandbox.withSandboxCsp(html, style + SIZE_SCRIPT),
    blocked: sandbox.externalRefs(html),
  };
}

// ── Naming what the user keeps ───────────────────────────────────────────

/** server/applications/artifacts.py _NAME_RE, character for character. */
const NAME_OK = /^[\p{L}\p{N}_ .,()\-–—'’"“”:;&!?+]{1,120}$/u;
const NAME_BAD = /[^\p{L}\p{N}_ .,()\-–—'’"“”:;&!?+]+/gu;

function cleanName(raw: string): string | null {
  const s = raw.replace(NAME_BAD, " ").replace(/\s+/g, " ").trim().slice(0, 120).trim();
  return s && NAME_OK.test(s) ? s : null;
}

/** A name for a visual the user saves: the fence's title="…", else the
 * document's <title>, else null (the caller dates it). Always one the
 * artifact service accepts. */
export function nameFor(html: string, meta?: string): string | null {
  const fromMeta = /title\s*=\s*"([^"]*)"/i.exec(meta || "")?.[1];
  const fromTitle = /<title[^>]*>([^<]*)<\/title>/i.exec(String(html).slice(0, 65536))?.[1];
  for (const c of [fromMeta, fromTitle]) {
    const n = c ? cleanName(c) : null;
    if (n) return n;
  }
  return null;
}

export function datedName(now: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `From chat ${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())} ${p(now.getHours())}:${p(now.getMinutes())}`;
}

/** A download file name: the name, filesystem-safe, .html. */
export function fileNameFor(name: string): string {
  const base = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);
  return `${base || "page"}.html`;
}
