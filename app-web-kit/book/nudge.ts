// Words sent into a running turn (a nudge), and the copy held until the turn
// reads them: which receipt settles which held copy, and when a line on
// screen is the nudge a frame, a receipt or a saved row names.
//
// A copy is held because the turn may end before it reads the words; then
// they go as the next message. The stream carries the runtime's receipt when
// the turn reads them, a 2:[{type: "nudge_read", text}] frame. A held copy the
// receipt matches has been read and must not also go as the next message, or
// the Host would be answered twice, the second time in a new turn from cold.
//
// Both apps hold their copies by these rules: the Workbench's page
// (use-workbench-chat, through lib/pending-sends.ts) and Circles'
// conversation (use-circles-chat nudgeRunningTurn). Pure, no React, so
// `node --test` pins it (workbench-web/test/pending-sends.test.ts and
// nudge.test.ts, through lib/pending-sends.ts; circles-web/test/
// nudge-held.test.ts).

/** A copy held until its thread's turn reads it. */
export interface HeldSend {
  /** The thread the words went into. */
  key: string;
  text: string;
  /** The id the words were nudged with (the nudge route's client_id): the
   * turn's receipt echoes it, so the held copy is settled by it, whatever the
   * receipt's text became (a secret shape cut from it). */
  clientId?: string;
}

/** A receipt with no id matches a held text by its opening: the receipt is
 * the text as read, after the platform's secret-shape screen, and the ask
 * card's answers all open with their ticket ("Ticket #N — …", "Not now on
 * ticket #N …"). */
export const readKey = (text: string): string => text.slice(0, 48);

/** How much of a nudge the turn registry's `nudge` frame carries: the words,
 * stripped, cut at 500 characters (turn_registry._nudge, `text[:500]`, a
 * Python slice: 500 code points, so an emoji counts once). */
export const NUDGE_FRAME_CHARS = 500;

/** A nudge's words as its `nudge` frame carries them: trimmed and cut the way
 * the server cuts them. Two texts are the same nudge when their keys are
 * equal, so a line on screen (the full words) and the frame for it (the cut
 * words) match at any length, and so do a line the frame drew and the user
 * row the turn saved when it read the words (the full words again). */
export function nudgeKey(text: string): string {
  return Array.from((text || "").trim()).slice(0, NUDGE_FRAME_CHARS).join("").trim();
}

/** Index of the held send a nudge_read receipt on thread `key` settles, or
 * -1. Only a send held for the same thread. A receipt that carries the
 * sender's id settles the send held with that id and nothing else; the text
 * opening is the rule only for a receipt with no id (the ask card's answer,
 * spoken into the turn by the decide, and older kernels). */
export function readIndex(
  pending: readonly HeldSend[],
  key: string,
  readText: string,
  clientId?: string | null,
): number {
  if (clientId) return pending.findIndex((p) => p.key === key && p.clientId === clientId);
  const seen = readText || "";
  if (!seen) return -1;
  return pending.findIndex((p) => p.key === key && !!p.text && seen.startsWith(readKey(p.text)));
}

/** Whether a nudge line on screen is the nudge a frame, a receipt or a saved
 * row names. One that carries the sender's id matches by the id alone: the
 * words it carries may not be the words sent (secret shapes are cut from a
 * nudge before the Host reads or stores it, session.push_nudge). One with no
 * id (another sender, an older kernel) matches by nudgeKey. */
export function sameNudge(
  line: { text: string; nudgeId?: string },
  server: { text: string; nudgeId?: string | null },
): boolean {
  if (server.nudgeId) return line.nudgeId === server.nudgeId;
  return nudgeKey(line.text) === nudgeKey(server.text);
}
