// Words typed into a running turn that the server took but did not write down:
// in PII mode the nudge route answers `held: false`, because no raw text may
// rest on the server. The page's copy is then the only one that outlives the
// turn. Page memory loses it on a reload, so the copy is kept in this tab's
// sessionStorage: the person's own device, one tab, cleared when the tab
// closes. It goes as soon as a turn has the words (the turn's nudge_read
// receipt, or the next message they go as is accepted). After a reload the
// page reads its kept words back and sends the ones no turn read. Words the
// server held (`held: true`) are never kept here.
//
// The same file is in circles-web/src/lib/kept-words.ts (each app's own:
// a change to app-web-kit/ would re-ship all four apps). Pure apart from the
// Storage it is handed, so `node --test` pins it.

import { redactSecrets } from "./secret-shapes.ts";

export interface KeptWords {
  /** The page the words were typed on (the hook's thread key). */
  key: string;
  text: string;
  /** The id the words were nudged with: the turn's receipt echoes it. */
  clientId: string;
  /** The turn the words went into, when the page knew it. */
  turnId: string | null;
  /** The page load that keeps them. Another load's are a reload's to send. */
  page: string;
  /** When they were sent (ISO). */
  at: string;
  /** What the page needs to send them as a message: Circles' target and
   * thread, the Workbench's project. */
  target?: { kind: string; id: string; label: string };
  threadId?: string | null;
  projectId?: number | null;
}

/** This tab's sessionStorage, or null where there is none (a private mode
 * that refuses it, a test). */
export function tabStore(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.sessionStorage : null;
  } catch {
    return null;
  }
}

/** The id words are nudged (and sent again) under: `prefix-<ms>-<random>`,
 * the shape history-merge.isLocalEcho knows. Random, not a page counter: the
 * server answers a second send under one id from the turn that has the words
 * (brain_service.send_message), so two pages must never make the same id. An
 * opaque tag; it carries none of the words. */
export function wordsId(prefix: string): string {
  let n = "";
  try {
    const a = new Uint32Array(2);
    globalThis.crypto.getRandomValues(a);
    n = `${a[0]}${String(a[1]).padStart(10, "0")}`;
  } catch {
    n = `${Math.floor(Math.random() * 1e9)}${Math.floor(Math.random() * 1e9)}`;
  }
  return `${prefix}-${Date.now()}-${n}`;
}

/** One id per page load: kept words with another id are a reload's. */
export function pageLoadId(): string {
  return `pg-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function readKept(store: Storage | null, name: string): KeptWords[] {
  if (!store) return [];
  try {
    const raw = JSON.parse(store.getItem(name) || "[]");
    return Array.isArray(raw)
      ? raw.filter(
          (w): w is KeptWords =>
            !!w && typeof w === "object" && typeof w.clientId === "string" && typeof w.text === "string" &&
            typeof w.key === "string",
        )
      : [];
  } catch {
    return [];
  }
}

function write(store: Storage | null, name: string, words: KeptWords[]): void {
  if (!store) return;
  try {
    if (words.length) store.setItem(name, JSON.stringify(words));
    else store.removeItem(name);
  } catch {
    /* full or refused: the page's own copy still holds them for this load */
  }
}

/** Keep `w` (replacing a copy under the same id). Hard secret shapes are cut
 * first: a card's note, or a key the composer's strip left in place, reaches a
 * nudge uncut, and the pod cuts it from the words anyway (session.push_nudge,
 * send_message), so what rests on the device is what the Host gets. */
export function keepWords(store: Storage | null, name: string, w: KeptWords): void {
  const cut = { ...w, text: redactSecrets(w.text).text };
  write(store, name, [...readKept(store, name).filter((x) => x.clientId !== w.clientId), cut]);
}

/** A turn has the words sent under `clientId`: their copy goes. */
export function dropKept(store: Storage | null, name: string, clientId: string): void {
  const cur = readKept(store, name);
  if (cur.some((x) => x.clientId === clientId)) write(store, name, cur.filter((x) => x.clientId !== clientId));
}

/** This page load takes over the copies under `ids` (it sends them now). */
export function adoptKept(store: Storage | null, name: string, ids: readonly string[], page: string): void {
  write(store, name, readKept(store, name).map((x) => (ids.includes(x.clientId) ? { ...x, page } : x)));
}

/** The sender ids a turn's stream (its data-stream text, replayed whole)
 * says the turn READ: its 2:[{type: "nudge_read", client_id}] receipts. */
export function readIdsIn(stream: string): Set<string> {
  const ids = new Set<string>();
  for (const line of stream.split("\n")) {
    if (!line.startsWith("2:") || !line.includes("nudge_read")) continue;
    try {
      const items = JSON.parse(line.slice(2));
      for (const it of Array.isArray(items) ? items : []) {
        if (it && it.type === "nudge_read" && typeof it.client_id === "string" && it.client_id) ids.add(it.client_id);
      }
    } catch {
      /* not a frame */
    }
  }
  return ids;
}
