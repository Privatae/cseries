// Secret shapes — the composer half of the pasted-credential gate.
//
// The rules are the ONE file at brain/mastercode/secret_shapes.json, shared
// with brain/mastercode/secret_shapes.py (the pod-side redaction at the top
// of send_message). This file mirrors that module's scan/redact contract so a
// hit named here is the hit the pod redacts. Keep the two TS copies
// (circles-web, workbench-web) byte-identical — scripts/test_secret_shapes.py
// asserts it.
//
// Nothing here leaves the browser: it runs on the draft text at send time and
// decides whether to hold the send behind a strip. Hard hits are redacted by
// the pod regardless of what the user picks; soft hits are the user's call.
import rulesDoc from "../../../brain/mastercode/secret_shapes.json" with { type: "json" };

export type Severity = "hard" | "soft";

export interface Finding {
  id: string;
  label: string;
  severity: Severity;
  start: number; // span of the SECRET (the redaction group), not the whole match
  end: number;
  preview: string; // 4 + … + 4, never the value
}

export interface KeysHint {
  name: string; // vault logical name — canonical provider name for vendor keys
  cred_type: "api_key" | "opaque";
}

interface RuleDoc {
  id: string;
  label: string;
  severity: Severity;
  pattern: string;
  flags?: string;
  group?: number;
  keys?: KeysHint;
}

interface EntropyDoc {
  id: string;
  label: string;
  severity: Severity;
  charset_pattern: string;
  min_length: number;
  min_bits_per_char: number;
}

const doc = rulesDoc as unknown as {
  version: number;
  redaction_format: string;
  rules: RuleDoc[];
  entropy?: EntropyDoc;
};

export const RULES_VERSION = doc.version;

const compiled = doc.rules.map((r) => ({
  id: r.id,
  label: r.label,
  severity: r.severity,
  group: r.group ?? 0,
  // "g" so exec() walks every match; "d" for indices so the group span is exact.
  re: new RegExp(r.pattern, `gd${r.flags ?? ""}`),
}));

const entropy = doc.entropy
  ? { ...doc.entropy, re: new RegExp(doc.entropy.charset_pattern, "g") }
  : null;

export function preview(value: string): string {
  if (value.length <= 12) return "•".repeat(Math.min(value.length, 8));
  return `${value.slice(0, 4)}…${value.slice(-4)}`;
}

export function shannonBitsPerChar(s: string): number {
  if (!s) return 0;
  const counts = new Map<string, number>();
  for (const ch of s) counts.set(ch, (counts.get(ch) ?? 0) + 1);
  const n = s.length;
  let bits = 0;
  for (const c of counts.values()) {
    const p = c / n;
    bits -= p * Math.log2(p);
  }
  return bits;
}

function entropyHit(token: string): boolean {
  if (!entropy || token.length < entropy.min_length) return false;
  if (!/[0-9]/.test(token) || !/[A-Za-z]/.test(token)) return false;
  return shannonBitsPerChar(token) >= entropy.min_bits_per_char;
}

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// The marker redactSecrets writes (and the pod's redact), for an id this rules
// file names. Only those: a person's "[REDACTED:sk-ant-…]" is not a marker, so
// a key typed inside brackets is still found. secret_shapes.py's _marker_re.
const markerRe = (() => {
  const ids = [...doc.rules.map((r) => r.id), ...(doc.entropy ? [doc.entropy.id] : [])];
  const at = doc.redaction_format.indexOf("{id}");
  if (at < 0) return new RegExp(escapeRe(doc.redaction_format), "g");
  const head = doc.redaction_format.slice(0, at);
  const tail = doc.redaction_format.slice(at + "{id}".length);
  return new RegExp(`${escapeRe(head)}(?:${ids.map(escapeRe).join("|")})${escapeRe(tail)}`, "g");
})();

/** `text` with each span replaced by NULs of the same length, so every span
 * still indexes the original. NUL is no rule's secret character and no word
 * character, as a marker's brackets are not. secret_shapes.py's _blank. */
function blank(text: string, spans: Array<[number, number]>): string {
  for (const [s, e] of spans) text = text.slice(0, s) + "\0".repeat(e - s) + text.slice(e);
  return text;
}

/** Every secret-shaped span, hard and soft, ordered by position. Overlaps
 * collapse to the first rule (JSON order is precedence).
 *
 * The same scan as the pod's (secret_shapes.py scan): a claimed span reads as
 * one opaque run to every rule after the one that claimed it. A marker an
 * earlier cut wrote is claimed before any rule runs, and a span a rule claims
 * is blanked before the next rule runs. So a link whose password slot is
 * already a marker warns of nothing, and a vendor token used as a link's user
 * name (a Telegram token holds a colon, a PEM block holds spaces) is named
 * together with the link's password, as the pod cuts both. */
export function scanSecrets(text: string): Finding[] {
  if (!text) return [];
  const taken: Array<[number, number]> = [];
  markerRe.lastIndex = 0;
  for (let m = markerRe.exec(text); m !== null; m = markerRe.exec(text)) {
    taken.push([m.index, m.index + m[0].length]);
  }
  const free = (a: number, b: number) => taken.every(([s, e]) => b <= s || a >= e);
  const out: Finding[] = [];
  let seen = blank(text, taken);

  for (const rule of compiled) {
    const claimed: Array<[number, number]> = [];
    rule.re.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = rule.re.exec(seen)) !== null) {
      if (m[0].length === 0) {
        rule.re.lastIndex++;
        continue;
      }
      const span = m.indices?.[rule.group];
      if (!span) continue;
      const [a, b] = span;
      if (a === b || !free(a, b)) continue;
      claimed.push([a, b]);
      out.push({
        id: rule.id,
        label: rule.label,
        severity: rule.severity,
        start: a,
        end: b,
        preview: preview(text.slice(a, b)),
      });
    }
    taken.push(...claimed);
    seen = blank(seen, claimed);
  }
  if (entropy) {
    entropy.re.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = entropy.re.exec(seen)) !== null) {
      const a = m.index;
      const b = a + m[0].length;
      const token = text.slice(a, b);
      if (!free(a, b) || !entropyHit(token)) continue;
      taken.push([a, b]);
      out.push({
        id: entropy.id,
        label: entropy.label,
        severity: entropy.severity,
        start: a,
        end: b,
        preview: preview(token),
      });
    }
  }
  out.sort((x, y) => x.start - y.start);
  return out;
}

/** Replace findings of the given severities with the redaction marker.
 * Returns ALL findings (including those left alone); spans are on the
 * ORIGINAL text. */
export function redactSecrets(
  text: string,
  severities: Severity[] = ["hard"],
): { text: string; findings: Finding[] } {
  const findings = scanSecrets(text);
  if (!findings.length) return { text, findings };
  let out = "";
  let cursor = 0;
  for (const f of findings) {
    if (!severities.includes(f.severity)) continue;
    out += text.slice(cursor, f.start);
    out += doc.redaction_format.replace("{id}", f.id);
    cursor = f.end;
  }
  out += text.slice(cursor);
  return { text: out, findings };
}

export const hardFindings = (f: Finding[]) => f.filter((x) => x.severity === "hard");
export const softFindings = (f: Finding[]) => f.filter((x) => x.severity === "soft");

/** One line for the strip: "Anthropic API key (sk-a…9xQ2)". */
export function describeFinding(f: Finding): string {
  return `${f.label} (${f.preview})`;
}

/** Where "Store in Keys" would seal this finding, or null for soft hits. */
export function keysHint(f: Finding): KeysHint | null {
  return doc.rules.find((r) => r.id === f.id)?.keys ?? null;
}

/** The secret bytes themselves — only ever handed to the vault call. */
export function secretValue(text: string, f: Finding): string {
  return text.slice(f.start, f.end);
}

/** Replace stored findings with a reference the Host can act on. */
export function replaceWithKeysRefs(text: string, stored: Array<{ f: Finding; name: string }>): string {
  const byStart = [...stored].sort((a, b) => a.f.start - b.f.start);
  let out = "";
  let cursor = 0;
  for (const { f, name } of byStart) {
    out += text.slice(cursor, f.start);
    out += `(stored in Keys as "${name}")`;
    cursor = f.end;
  }
  return out + text.slice(cursor);
}

// ── Per-browser memory of what was already handled ────────────────────────
//
// Asking again about a value already handled would get annoying, so it
// remembers, without a DB: the value's SHA-256 (first 16 hex
// chars — not reversible for key material) maps to the decision the user
// made in the strip, in this browser's localStorage. A key already sealed
// into Keys is swapped for its (stored in Keys as "…") reference on sight; a
// soft hit already sent as-is is not held again. Nothing here changes the
// pod wall — it has no memory and still redacts every hard shape.
//
// It only learns from the strip: a key stored through the Keys app directly,
// or on another browser, is unknown here and is held like any new one.

export type Remembered = { kind: "stored"; name: string } | { kind: "allowed" };

const MEMORY_KEY = "cseries.secret-shapes.decisions.v1";
const MEMORY_CAP = 200;

// Test seam: node has no localStorage; a plain object stands in.
let memoryFallback: Record<string, Remembered> = {};

function readMemory(): Record<string, Remembered> {
  try {
    if (typeof localStorage === "undefined") return memoryFallback;
    const raw = localStorage.getItem(MEMORY_KEY);
    return raw ? (JSON.parse(raw) as Record<string, Remembered>) : {};
  } catch {
    return {};
  }
}

function writeMemory(m: Record<string, Remembered>): void {
  const keys = Object.keys(m);
  if (keys.length > MEMORY_CAP) for (const k of keys.slice(0, keys.length - MEMORY_CAP)) delete m[k];
  try {
    if (typeof localStorage === "undefined") {
      memoryFallback = m;
      return;
    }
    localStorage.setItem(MEMORY_KEY, JSON.stringify(m));
  } catch {
    /* quota / private mode: forgetting is the safe failure */
  }
}

export function forgetAllDecisions(): void {
  writeMemory({});
}

export async function fingerprint(value: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(buf).slice(0, 8), (b) => b.toString(16).padStart(2, "0")).join("");
}

export async function rememberDecision(value: string, d: Remembered): Promise<void> {
  const m = readMemory();
  m[await fingerprint(value)] = d;
  writeMemory(m);
}

export interface MemoryOutcome {
  text: string; // stored keys swapped for their Keys reference
  remaining: Finding[]; // findings (on the NEW text) that still need the user
  notes: string[]; // one line per swap, for a quiet line under the composer
}

/** Apply remembered decisions to a draft before deciding whether to hold it. */
export async function applyMemory(text: string, findings: Finding[]): Promise<MemoryOutcome> {
  if (!findings.length) return { text, remaining: [], notes: [] };
  const m = readMemory();
  const stored: Array<{ f: Finding; name: string }> = [];
  const allowed = new Set<string>();
  for (const f of findings) {
    const d = m[await fingerprint(secretValue(text, f))];
    if (!d) continue;
    if (d.kind === "stored") stored.push({ f, name: d.name });
    else allowed.add(f.id + ":" + f.start);
  }
  if (!stored.length && !allowed.size) return { text, remaining: findings, notes: [] };
  const out = stored.length ? replaceWithKeysRefs(text, stored) : text;
  // Offsets moved if anything was swapped: re-scan the new text and drop the
  // allowed ones by value, not by position.
  const rescanned = stored.length ? scanSecrets(out) : findings;
  const remaining: Finding[] = [];
  for (const f of rescanned) {
    const d = m[await fingerprint(secretValue(out, f))];
    if (d?.kind === "allowed") continue;
    remaining.push(f);
  }
  return {
    text: out,
    remaining,
    notes: stored.map(({ f, name }) => `Replaced a ${f.label} you already stored in Keys as "${name}".`),
  };
}
