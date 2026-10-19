import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
} from "react";
import {
  HOLD_SHOW_MS,
  LEVEL_WORDS,
  holdProgress,
  levelAt,
} from "./hold-to-think.ts";

/** What the composer drives from its own keys: holding Enter in the box is
 * holding the press. Each acts only on a hold the box started, so the box
 * losing the focus, or an Enter let go there, never ends a hold of the
 * button itself. */
export interface PressHandle {
  /** Enter went down in the box. */
  start: () => void;
  /** Enter came up in the box: sends at the level reached (null: a tap). */
  release: () => void;
  /** The box lost the focus: the hold ends, sending nothing. */
  cancel: () => void;
}

/**
 * The book's Send, "the ink button" of the mockup, with hold-to-think: a tap
 * sends at the page's effort; held, it climbs through `climb` (high, then max, the ones
 * the page's model takes), shows the level it has reached above the button,
 * and letting go sends at that level. The timings are book/hold-to-think.ts's.
 *
 * Pointer and touch: press and hold (a touch long-press; the browser's own
 * long-press menu is held off). Keyboard: Space or Enter held on the button,
 * or Enter held in the box (the composer calls start/release). A click from
 * an assistive tool, with no press before it, is a tap. Letting go off the
 * button, or the browser taking the touch, sends nothing.
 */
export const PressButton = forwardRef<
  PressHandle,
  {
    /** The model's levels, low to high: the dots round the button. */
    levels: readonly string[];
    /** The page's effort: the dots lit at rest. */
    level: string | null;
    climb: readonly string[];
    disabled: boolean;
    onSend: (reached: string | null) => void;
    describedBy?: string;
  }
>(function PressButton({ levels, level, climb, disabled, onSend, describedBy }, ref) {
  const [heldMs, setHeldMs] = useState<number | null>(null);
  const startedRef = useRef<number | null>(null);
  // Who holds the press: the box (Enter held there) or the button itself.
  const byRef = useRef<"box" | "button" | null>(null);
  const rafRef = useRef<number | null>(null);
  const climbRef = useRef(climb);
  climbRef.current = climb;
  const sendRef = useRef(onSend);
  sendRef.current = onSend;

  const stopClock = () => {
    if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
  };
  const tick = useCallback(() => {
    if (startedRef.current == null) return;
    setHeldMs(performance.now() - startedRef.current);
    rafRef.current = requestAnimationFrame(tick);
  }, []);
  const start = useCallback((by: "box" | "button") => {
    if (startedRef.current != null) return;
    startedRef.current = performance.now();
    byRef.current = by;
    setHeldMs(0);
    rafRef.current = requestAnimationFrame(tick);
  }, [tick]);
  const finish = useCallback((send: boolean) => {
    const t0 = startedRef.current;
    if (t0 == null) return;
    startedRef.current = null;
    byRef.current = null;
    stopClock();
    setHeldMs(null);
    if (send) sendRef.current(levelAt(performance.now() - t0, climbRef.current));
  }, []);
  useImperativeHandle(
    ref,
    () => ({
      start: () => start("box"),
      release: () => {
        if (byRef.current === "box") finish(true);
      },
      cancel: () => {
        if (byRef.current === "box") finish(false);
      },
    }),
    [start, finish],
  );
  useEffect(() => () => stopClock(), []);
  // A press cannot outlive the button being usable (the turn started, the
  // draft was cleared elsewhere).
  useEffect(() => {
    if (disabled) finish(false);
  }, [disabled, finish]);

  const onPointerDown = (e: PointerEvent<HTMLButtonElement>) => {
    if (disabled || e.button !== 0) return;
    e.preventDefault();
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* the pointer is already gone */
    }
    start("button");
  };
  const onPointerUp = (e: PointerEvent<HTMLButtonElement>) => {
    if (startedRef.current == null) return;
    const r = e.currentTarget.getBoundingClientRect();
    const inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
    finish(inside);
  };
  const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (e.key !== " " && e.key !== "Enter") return;
    e.preventDefault();
    if (e.repeat || disabled) return;
    start("button");
  };
  const onKeyUp = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (e.key !== " " && e.key !== "Enter") return;
    e.preventDefault();
    finish(true);
  };

  const holding = heldMs != null && heldMs >= HOLD_SHOW_MS;
  const reached = heldMs != null ? levelAt(heldMs, climb) : null;
  const progress = heldMs != null ? holdProgress(heldMs, climb) : 0;
  const shownLevel = reached ?? level;
  const lit = shownLevel ? levels.indexOf(shownLevel) : -1;
  const R = 18;
  const C = 2 * Math.PI * (R + 3);

  return (
    <div className="book-press-wrap">
      {reached && (
        <div aria-live="polite" className="book-lvl" data-level={reached} role="status">
          <b>{reached.toUpperCase()}</b>
          {LEVEL_WORDS[reached] ?? ""}
        </div>
      )}
      <button
        aria-describedby={describedBy}
        aria-label={climb.length ? "Send. Hold to think harder" : "Send"}
        className="book-press"
        data-holding={holding || undefined}
        data-press
        disabled={disabled}
        onClick={(e) => {
          // A press already sent on letting go: the click after a pointer's
          // press has detail >= 1, and the keys' own click is held off
          // (preventDefault above). A click with no press before it (detail
          // 0: a screen reader's activation) is a tap.
          if (!disabled && e.detail === 0 && startedRef.current == null) sendRef.current(null);
        }}
        onContextMenu={(e) => e.preventDefault()}
        onKeyDown={onKeyDown}
        onKeyUp={onKeyUp}
        onLostPointerCapture={() => {
          if (startedRef.current != null) finish(false);
        }}
        onPointerCancel={() => finish(false)}
        onPointerDown={onPointerDown}
        onPointerUp={onPointerUp}
        type="button"
      >
        <svg aria-hidden viewBox="-24 -24 48 48">
          {holding && (
            <circle
              className="book-press-ring"
              fill="none"
              r={R + 3}
              strokeDasharray={`${C * progress} ${C}`}
              strokeWidth={3}
              transform="rotate(-90)"
            />
          )}
          <circle className="book-press-ink" r={holding ? 6 + 9 * progress : 7} />
          {levels.map((lv, i) => {
            const a = ((-90 + (i * 360) / Math.max(1, levels.length)) * Math.PI) / 180;
            return (
              <circle
                className="book-press-dot"
                cx={R * Math.cos(a)}
                cy={R * Math.sin(a)}
                data-on={i <= lit || undefined}
                key={lv}
                r={1.5}
              />
            );
          })}
        </svg>
      </button>
    </div>
  );
});
