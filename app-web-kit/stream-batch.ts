// Folds a streamed reply into at most one state update per animation frame.
//
// The stream client hands every text delta to the chat hook, and each one used
// to become a React state update — a full commit of the thread per 20-40
// characters. The screen cannot show more than one frame's worth anyway, so the
// hook now records the latest text here and applies it once per frame.
//
// Ordering: any other frame (tool call, tool result, error, finish) must call
// flush() first, so the text that preceded it lands before it does. A 100 ms
// timer backs up requestAnimationFrame, which a hidden or background window
// does not run — the text still arrives, just without per-frame pacing.

export interface TextBatcher {
  text(full: string): void;
  reasoning(full: string): void;
  flush(): void;
}

export function createTextBatcher(
  apply: (text: string | null, reasoning: string | null) => void,
): TextBatcher {
  let text: string | null = null;
  let reasoning: string | null = null;
  let raf = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const hasRaf = typeof requestAnimationFrame === "function";

  const flush = () => {
    if (raf) {
      cancelAnimationFrame(raf);
      raf = 0;
    }
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
    if (text === null && reasoning === null) return;
    const t = text;
    const r = reasoning;
    text = null;
    reasoning = null;
    apply(t, r);
  };

  const schedule = () => {
    if (raf || timer !== null) return;
    if (hasRaf) raf = requestAnimationFrame(flush);
    timer = setTimeout(flush, 100);
  };

  return {
    text(full) {
      text = full;
      schedule();
    },
    reasoning(full) {
      reasoning = full;
      schedule();
    },
    flush,
  };
}
