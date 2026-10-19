// What a support draft's Send names (the Tickets pane's rule, kept by the
// Support card's ticket in full, src/cards/tickets.tsx, when the rail went).

/**
 * The draft version a Send names: the one on the person's screen. A ticket
 * read without one (a list row standing in for a failed read) names 0, so a
 * draft the Host has rewritten since is refused rather than sent unseen.
 */
export function sendRevision(ticket: { revision?: number | null }): number {
  return typeof ticket.revision === "number" ? ticket.revision : 0;
}
