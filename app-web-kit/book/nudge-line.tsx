/** Words the person sent into a running turn, on a page of the book: a line
 * of its own, "NUDGE · 21:41", under the turn it went into, and "read by the
 * Host" once the turn has read them. The app says when they were sent (its
 * own clock's words) and whether they were read: the turn's receipt, or the
 * row the turn saved when it read them; until then they wait, and words the
 * turn never reads give way to the next message's own line. */
export function NudgeLine({ text, clock, read }: { text: string; clock: string; read: boolean }) {
  return (
    <div className="book-nudge" data-nudge-line data-nudge-read={read ? "" : undefined}>
      <b>NUDGE{clock ? ` · ${clock}` : ""}{read ? " · read by the Host" : ""}</b>
      <span>{text}</span>
    </div>
  );
}
