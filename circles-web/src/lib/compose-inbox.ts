// What another app hands your Host's chat: `cseries:compose` (the shell relays
// it, same origin only). mode 'pill' rides
// the next send; mode 'send' goes now as the person's own words (the
// Orchestrator's Connect seed, dashboard/base/js/app-runner.js).
//
// Your Host's composer is drawn only on your card's back, and a phone starts
// on the deck, so a listener inside the composer missed every message that
// arrived while the deck or another card was showing. This listener is
// installed from main.tsx before the first render: a message that finds no
// composer is held, the cards are asked to open your card's back, and the
// composer takes it once it is drawn. The page marks itself ready for the
// shell (COMPOSE_READY_ATTR), so a new window is handed its seed without
// waiting for a text box the deck does not have.

export interface ComposeMessage {
  mode: "pill" | "send";
  text: string;
}

/** The attribute on <html> the shell reads (app-runner.js
 * deliverPromptWhenReady): this page holds a compose until it can use it. */
export const COMPOSE_READY_ATTR = "data-compose-inbox";
/** Held at most this many, oldest dropped (the composer keeps five pills). */
const HOLD_MAX = 5;

let installed = false;
const held: ComposeMessage[] = [];
const takers = new Set<(m: ComposeMessage) => void>();
const wanters = new Set<() => void>();

/** A valid compose from a message event, or null. */
export function composeOf(ev: { origin: string; data: unknown }, origin: string): ComposeMessage | null {
  if (ev.origin !== origin) return null;
  const d = ev.data as { type?: unknown; mode?: unknown; text?: unknown } | null;
  if (!d || d.type !== "cseries:compose") return null;
  if (typeof d.text !== "string" || !d.text.trim() || d.text.length > 2000) return null;
  return { mode: d.mode === "send" ? "send" : "pill", text: d.text.trim() };
}

function deliver(m: ComposeMessage): void {
  if (takers.size > 0) {
    // One composer is drawn at a time; a send must go once.
    const first = takers.values().next().value as (m: ComposeMessage) => void;
    first(m);
    return;
  }
  held.push(m);
  if (held.length > HOLD_MAX) held.shift();
  wanters.forEach((fn) => fn());
}

/** Listen from now on. Idempotent; call it before the first render. */
export function installComposeInbox(): void {
  if (installed || typeof window === "undefined") return;
  installed = true;
  document.documentElement.setAttribute(COMPOSE_READY_ATTR, "1");
  window.addEventListener("message", (ev: MessageEvent) => {
    const m = composeOf(ev, window.location.origin);
    if (m) deliver(m);
  });
}

/** The composer takes messages from now on, the held ones first. Returns the
 * unsubscribe. */
export function onCompose(fn: (m: ComposeMessage) => void): () => void {
  installComposeInbox();
  takers.add(fn);
  while (held.length > 0 && takers.has(fn)) fn(held.shift() as ComposeMessage);
  return () => {
    takers.delete(fn);
  };
}

/** Called when a message is waiting for the composer (and at once, if one
 * already is): the cards open your card's back. Returns the unsubscribe. */
export function onComposeWanted(fn: () => void): () => void {
  installComposeInbox();
  wanters.add(fn);
  if (held.length > 0) fn();
  return () => {
    wanters.delete(fn);
  };
}

/** Tests only: forget the listener and anything held or subscribed. */
export function resetComposeInboxForTests(): void {
  installed = false;
  held.length = 0;
  takers.clear();
  wanters.clear();
}
