import type { ChatMessage, DeliveryEvent } from "./types";
import { sameNudge } from "./pending-sends.ts";

/** True when the id was minted by localId() (an optimistic echo no server row
 * backs yet). Server-backed ids — numeric turn-row ids, UUIDs — never match
 * this shape. The history merge keys on this distinction. */
export const isLocalEcho = (id: string): boolean => /^[a-z]+-\d+-\d+$/.test(id);

/** Does `rows` contain the turn this delivery announced?
 *
 * The delivery poll's success test, kept pure so it can be pinned. It must be
 * the NAMED row — the entity_memory id the kernel wrote, or failing that the
 * delivering task's id — and never "the window contains something new".
 *
 * The "something new" test was the bug: this
 * client never swaps its own just-streamed turn's optimistic echoes for their
 * server rows, so the user's own last exchange came back looking new to the
 * first delta fetch after any trigger. The bench fires on `delivered_at`,
 * which the kernel stamps BEFORE the report's phrasing model call — so the
 * loop fetched into that ~7s gap, saw the user's own turn, declared success
 * and quit. The report was written a moment later and nothing looked again.
 *
 * An UNNAMED delivery (a socket reopen, which says "something may have landed"
 * without saying what) has no row to wait for; callers fall back to "grew".
 */
export function deliveryLanded(
  rows: ChatMessage[],
  delivery: Pick<DeliveryEvent, "rowId" | "taskId">,
): boolean {
  const { rowId, taskId } = delivery;
  if (rowId != null) return rows.some((m) => m.id === String(rowId));
  if (taskId != null) return rows.some((m) => m.agentTaskId === taskId);
  return false;
}

/** True when this delivery names a row to wait for. */
export const deliveryIsNamed = (
  delivery: Pick<DeliveryEvent, "rowId" | "taskId">,
): boolean => delivery.rowId != null || delivery.taskId != null;

/** Is the thread on screen the one this delivery's row was written into?
 *
 * A NAMED delivery belongs to exactly one thread, so polling any other one
 * can only ever time out — and then report "the report could not be shown
 * here" about a report that landed correctly somewhere else. A lie on this
 * surface is worse than the silence it replaced, so the match must be exact:
 * project↔project, and General (both null) only with General.
 *
 * A Circles-origin dispatch carries no project and is not ours to wait for;
 * `appId` is the frame's origin surface. Null means the bench poll, which is
 * the Workbench's own.
 *
 * An UNNAMED delivery (socket reopen) names no thread and no row — it is a
 * bare "something may have landed", valid against whatever is on screen.
 */
export function deliveryTargetsThread(
  delivery: Pick<DeliveryEvent, "rowId" | "taskId" | "projectId" | "appId">,
  projectId: number | null,
): boolean {
  if (!deliveryIsNamed(delivery)) return true;
  if (delivery.appId != null) return delivery.appId === "workbench" && delivery.projectId === projectId;
  // No origin surface on a NAMED delivery: a row written before the bench
  // carried `origin_app_id` (2026-09-05). Its report may be in Circles or in
  // another project, and we cannot tell — so a project thread is a plain
  // mismatch and General, which would otherwise match null, must not claim it
  // either. Unattributable means "not mine": the socket path still covers it,
  // and staying quiet beats claiming a report failed when it did not.
  if (delivery.projectId == null) return false;
  return delivery.projectId === projectId;
}

/** What one delta fetch settles: whether to adopt the window, and whether the
 * wait is over. Pure so the LOOP's decision is pinned, not just its helper —
 * the bug was never in a predicate, it was in what the loop accepted as done.
 *
 * `adopt` is "the window carries rows we don't have" (the never-wipe merge
 * handles the rest). `done` is "the named row is here" — or, for an unnamed
 * delivery with nothing to wait for, the old "it grew" behaviour. */
export function evaluateDeliveryFetch(
  onScreen: ChatMessage[],
  fetched: ChatMessage[],
  delivery: Pick<DeliveryEvent, "rowId" | "taskId">,
): { adopt: boolean; done: boolean } {
  const knownIds = new Set(onScreen.map((m) => m.id));
  const grew = fetched.some((m) => !knownIds.has(m.id));
  const done = deliveryIsNamed(delivery) ? deliveryLanded(fetched, delivery) : grew;
  return { adopt: grew, done };
}

/**
 * Merge a fetched history window into what's on screen, preserving OBJECT
 * IDENTITY for turns the server re-sent unchanged. This is the whole fix for
 * "the conversation reloads between responses": with server turn ids as React
 * keys and unchanged rows kept as the same object, adopting a refetch is a
 * no-op diff — React touches only the turns that actually changed.
 *
 * Shape handled:
 *   [older local rows outside the fetched window] ++ [the fetched window,
 *   identity-preserved] ++ [optimistic tail the server hasn't persisted yet]
 *
 * The same merge therefore serves full-window refetches AND `since=` delta
 * fetches (where `server` holds only the newest few rows).
 *
 * Never-wipe: a window whose newest row is OLDER than the newest server-backed
 * row on screen is a stale read — ignored. Optimistic-tail rows are dropped
 * only when a server row with the same role and text has arrived (the write
 * landed; the real row replaces the echo and carries the durable id).
 * Twin: circles-web/src/lib/history-merge.ts.
 */
export function mergeHistory(
  local: ChatMessage[],
  server: ChatMessage[],
): ChatMessage[] {
  if (local.length === 0) return server;
  if (server.length === 0) return local;

  const newestBacked = (() => {
    for (let i = local.length - 1; i >= 0; i--) {
      if (!isLocalEcho(local[i].id)) return local[i];
    }
    return null;
  })();
  const serverNewest = server[server.length - 1];
  if (newestBacked && serverNewest.createdAt < newestBacked.createdAt) {
    return local; // stale window — never adopt backwards
  }

  const byId = new Map(local.map((m) => [m.id, m]));
  const serverIds = new Set(server.map((m) => m.id));

  // Server-backed rows on screen that fell outside this window (it's a
  // newest-N or delta fetch) — keep them; the reload must not shrink the top.
  const firstServer = server[0];
  const olderPrefix = local.filter(
    (m) =>
      !isLocalEcho(m.id) &&
      !serverIds.has(m.id) &&
      m.createdAt < firstServer.createdAt,
  );

  const merged = server.map((s) => {
    const l = byId.get(s.id);
    return l && l.text === s.text && l.tools.length === s.tools.length ? l : s;
  });

  // The echoes on screen: the trailing run (a send or reattach echo) plus any
  // echo already placed with its turn by an earlier merge (below) — collecting
  // the trailing run alone dropped those one refresh later.
  const tail: ChatMessage[] = [];
  let trailing = true;
  for (let i = local.length - 1; i >= 0; i--) {
    const m = local[i];
    if (!isLocalEcho(m.id)) {
      trailing = false;
      continue;
    }
    if (trailing || m.turnToken != null) tail.unshift(m);
  }
  // An echo is replaced by the saved row of the SAME TURN (turnToken + role).
  // Text equality is only the fallback for rows saved before the token was.
  // On the kernels that wrote those rows the saved reply was not always the
  // streamed one (text streamed ahead of a tool call was left out), and an
  // echo that never matched stayed on screen for good — a second copy, pinned
  // below every newer message. A row saved now holds every piece the turn
  // sent, exactly as streamed (session.py records each piece as it is sent).
  // A nudge is saved as a user row when the running turn reads it
  // (session.py's drain): that row replaces
  // its echo; until it is there the echo stays beside its turn. Matched by
  // nudgeKey: a line the registry's nudge frame drew holds the words cut at
  // 500 characters, and the saved row holds them whole.
  const savedTurns = new Set(
    server.filter((s) => s.turnToken).map((s) => `${s.role}:${s.turnToken}`),
  );
  const recent = server.slice(-10);
  const keep = tail.filter((m) => {
    if (m.streaming) return true;
    if (m.nudge) {
      // By the sender's id when the row carries one (a secret shape cut from
      // the saved words would never match them by text).
      return !recent.some((s) => s.role === "user" && s.nudge && sameNudge(m, s));
    }
    // Its own turn's row is saved: the row replaces it, even when the bubble
    // errored (a connection that died while the turn finished server-side).
    if (m.turnToken != null && savedTurns.has(`${m.role}:${m.turnToken}`)) return false;
    return (
      m.error != null ||
      !recent.some((s) => s.role === m.role && s.text.trim() === m.text.trim())
    );
  });

  // An echo that stays sits WITH ITS TURN, not at the bottom: ahead of the
  // turn's saved reply when there is one (a nudge, an errored bubble), else
  // after the turn's last row (a reply stopped before any row was written).
  // Echoes with no turn on screen keep the tail, in order.
  const out = [...olderPrefix, ...merged];
  const loose: ChatMessage[] = [];
  for (const m of keep) {
    const t = m.turnToken;
    if (t != null) {
      const reply = out.findIndex(
        (r) => r.turnToken === t && r.role === "assistant" && !isLocalEcho(r.id),
      );
      if (reply >= 0) {
        out.splice(reply, 0, m);
        continue;
      }
      let last = -1;
      out.forEach((r, i) => {
        if (r.turnToken === t) last = i;
      });
      if (last >= 0) {
        out.splice(last + 1, 0, m);
        continue;
      }
    }
    loose.push(m);
  }
  return [...out, ...loose];
}

/** Stamp a turn's token on its assistant echo, on the user echo that prompted
 * it, and on any nudge bubbles drawn between the two. Only local echoes are
 * touched, and only ones without a token; a saved row carries its own. */
export function stampTurnToken(
  messages: ChatMessage[],
  asstId: string,
  token: string,
): ChatMessage[] {
  const idx = messages.findIndex((m) => m.id === asstId);
  if (idx < 0) return messages;
  const stamp = new Set<number>();
  if (messages[idx].turnToken !== token) stamp.add(idx);
  for (let i = idx - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role !== "user" || !isLocalEcho(m.id)) break;
    if (m.turnToken == null) stamp.add(i);
    if (!m.nudge) break; // the message that prompted the turn
  }
  if (stamp.size === 0) return messages;
  return messages.map((m, i) => (stamp.has(i) ? { ...m, turnToken: token } : m));
}

/** Keys for saved rows that arrive with no id (the circle branch of Circles'
 * get_thread sends none). Built from the row itself — time, writer, text —
 * plus a count for exact repeats, so a row keeps its key across fetches even
 * when the window slides, and the key is never echo-shaped (isLocalEcho would
 * take the row for an unsaved echo and re-append it on every fetch). */
export function historyKeys(
  rows: Array<{ createdAt?: string | null; author?: string | null; text?: string | null }>,
): string[] {
  const seen = new Map<string, number>();
  return rows.map((r) => {
    let h = 5381;
    const body = r.text ?? "";
    for (let i = 0; i < body.length; i++) h = ((h * 33) ^ body.charCodeAt(i)) >>> 0;
    // Time and writer are escaped, so neither can carry a separator into the
    // next field; repeats take `#n`, which no base36 hash contains.
    const base = `h:${encodeURIComponent(r.createdAt ?? "")}:${encodeURIComponent(r.author ?? "")}:${h.toString(36)}`;
    const n = seen.get(base) ?? 0;
    seen.set(base, n + 1);
    return n === 0 ? base : `${base}#${n}`;
  });
}
