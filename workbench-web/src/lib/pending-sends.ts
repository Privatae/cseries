// The ask card's answer, held until its thread goes idle (use-workbench-chat
// PENDING_SENDS) — unless the running turn READ it first.
//
// Deciding a ticket speaks the answer into the turn that asked
// (tickets.decide_ticket_service -> session.answer_into_turn), and that turn
// is holding for it at its exit, so the answer reaches the Host in the same
// turn. The stream then carries the runtime's receipt, a 2:[{type:
// "nudge_read", text}] frame. A held copy the receipt matches has been read
// and must not also go as the next message, or the Host would be answered
// twice, the second time in a new turn from cold.
//
// The matching rules are the book's, shared with Circles
// (app-web-kit/book/nudge.ts). Imported by a relative path: plain
// `node --test` cannot read the app's @kit alias.

import type { HeldSend } from "../../../app-web-kit/book/nudge.ts";

export { NUDGE_FRAME_CHARS, nudgeKey, readIndex, readKey, sameNudge } from "../../../app-web-kit/book/nudge.ts";

export interface PendingSend extends HeldSend {
  projectId: number | null;
}
