// Turn types — where a message LANDED.
//
// The platform decides which type a message is before the turn and answers
// the send with `x-landed-thread` (always, for a self send) and, when a type
// took it, `x-landed-type` / `x-landed-type-name`. The view follows the
// reply: when the landed thread is not the one the message was sent into,
// the pane moves there — no click, no "answered in X" chip. This file is the
// pure reading of those headers so `node --test` pins it without a DOM.

import type { FeedTopic } from "./types";

export const ORIGINAL_THREAD = "original";

export interface Landing {
  /** The thread the message landed in ("original" | uuid). */
  threadId: string;
  /** The type that took it, when one did. */
  typeId: string | null;
  typeName: string | null;
  /** The server's clock when the message landed (ISO), when the server says:
   * every landed-thread row saved at or after it is this turn. */
  at: string | null;
  /** True when the landed thread differs from the one the message was sent
   * into — the view should move. */
  moved: boolean;
}

interface HeadersLike {
  get(name: string): string | null;
}

/** Normalise a client-side thread id the way the server does: absent, null,
 * "original" and the historic "self" all mean the original conversation. */
export function normalizeThreadId(id: string | null | undefined): string {
  const v = (id ?? "").trim();
  if (!v || v === ORIGINAL_THREAD || v === "self") return ORIGINAL_THREAD;
  return v;
}

/** Read where the send landed. Null when the response carries no landing
 * header (a non-self send, or a server that predates turn types) — the
 * caller then behaves exactly as before. */
export function landingFrom(
  headers: HeadersLike,
  sentThreadId: string | null | undefined,
): Landing | null {
  const landed = (headers.get("x-landed-thread") ?? "").trim();
  if (!landed) return null;
  const threadId = normalizeThreadId(landed);
  const typeId = (headers.get("x-landed-type") ?? "").trim() || null;
  const typeName = (headers.get("x-landed-type-name") ?? "").trim() || null;
  const at = (headers.get("x-landed-at") ?? "").trim() || null;
  return {
    threadId,
    typeId,
    typeName,
    at,
    moved: threadId !== normalizeThreadId(sentThreadId),
  };
}

/** Where THIS turn begins in a landed thread's saved history: the index of
 * the LAST user row, when its text equals the message being sent (the turn
 * writes that row at its start, the reply at its end). Everything from there
 * on is this turn's; everything before is the thread's earlier history.
 * history.length when this turn is not in the history yet. */
export function thisTurnStart(
  history: ReadonlyArray<{ role: string; text: string; createdAt?: string }>,
  sentText: string | null | undefined,
  landedAt?: string | null,
): number {
  // The server's landing moment decides when it is known: the first row
  // saved at or after it. Identical text can never fool it.
  const at = landedAt ? Date.parse(landedAt) : NaN;
  if (!Number.isNaN(at)) {
    for (let i = 0; i < history.length; i++) {
      const t = Date.parse(history[i].createdAt ?? "");
      if (!Number.isNaN(t) && t >= at) return i;
    }
    return history.length;
  }
  const sent = (sentText ?? "").trim();
  if (!sent) return history.length;
  for (let i = history.length - 1; i >= 0; i--) {
    if (history[i].role === "user") {
      return history[i].text.trim() === sent ? i : history.length;
    }
  }
  return history.length;
}

/** The pane's thread key for a self thread — the shape use-circles-chat keys
 * its live stream on. */
export function selfThreadKey(threadId: string | null | undefined): string {
  return `self:${normalizeThreadId(threadId)}`;
}

/** The topic a local echo belongs to, for your Host's one conversation: a
 * thread id as a FeedTopic, none for the original conversation (it has no
 * rows) or an id not known yet. Your Host's back shows one topic at a time,
 * so every echo says which one it is in. */
export function topicTag(id: string | null | undefined, title = ""): FeedTopic | undefined {
  const t = normalizeThreadId(id);
  return t === ORIGINAL_THREAD ? undefined : { id: t, title };
}
