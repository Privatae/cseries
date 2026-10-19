import { useEffect, useRef, type ReactNode } from "react";
import type { TabLight } from "@kit/book/rules";
import { initialOf } from "@/lib/cards";

/** A colour of the desktop's for a name, the same every time: a person and
 * their Host share one, so a pair reads as a pair (mockup:38-39). */
export function hueOf(seed: string): number {
  let h = 0;
  for (const ch of seed) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return 1 + (h % 6);
}

/** A face: a person round, a Host square (decision 9; mockup:836). */
export function Face({
  name,
  host,
  size = 32,
  hue,
  big,
}: {
  name: string;
  host?: boolean;
  size?: number;
  hue: number;
  big?: boolean;
}) {
  return (
    <span
      aria-label={`${name}${host ? " (a Host)" : " (a person)"}`}
      className={`av ${host ? "host" : "person"} hue-${hue}${big ? " big" : ""}`}
      data-face={host ? "host" : "person"}
      role="img"
      style={{ ["--s" as string]: `${size}px` }}
    >
      {initialOf(name)}
    </span>
  );
}

/** Support's face: the platform's team, not a person you know. */
export function TeamFace({ size = 36 }: { size?: number }) {
  return (
    <span
      aria-label="The platform's support team"
      className="av team"
      role="img"
      style={{ ["--s" as string]: `${size}px` }}
    >
      ?
    </span>
  );
}

export function Light({ light, fresh, title }: { light: TabLight; fresh?: boolean; title?: string }) {
  if (!light) return null;
  return (
    <span
      aria-label={title ?? (light === "amber" ? "waits on you" : "something landed")}
      className={`light ${light}${fresh ? " new" : ""}`}
      data-light={light}
      role="img"
      title={title}
    />
  );
}

export function Working({ label = "working" }: { label?: string }) {
  return (
    <span aria-label={label} className="working" data-working role="img">
      <i />
      <i />
      <i />
    </span>
  );
}

/** The folded corner that turns a card over (mockup:385). */
export function Corner({
  label,
  face,
  light,
  onTurn,
  turnTo,
}: {
  label: string;
  face?: ReactNode;
  light?: TabLight;
  onTurn: () => void;
  turnTo: "front" | "back";
}) {
  return (
    <button
      aria-label={`Turn the card over, to its ${turnTo}: ${label}`}
      className="corner"
      data-corner
      onClick={onTurn}
      title="Turn the card over"
      type="button"
    >
      {light ? <Light light={light} /> : null}
      <span className="lab">
        {face}
        {label} ↻
      </span>
    </button>
  );
}

/** A popover that closes on Escape and on a click outside it. */
export function Pop({
  children,
  onClose,
  style,
  label,
}: {
  children: ReactNode;
  onClose: () => void;
  style?: React.CSSProperties;
  label: string;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const down = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    // A tick later, so the click that opened it does not close it.
    const t = window.setTimeout(() => {
      document.addEventListener("mousedown", down);
      document.addEventListener("keydown", key);
    }, 0);
    return () => {
      window.clearTimeout(t);
      document.removeEventListener("mousedown", down);
      document.removeEventListener("keydown", key);
    };
  }, [onClose]);
  return (
    <div aria-label={label} className="pop" ref={ref} role="dialog" style={style}>
      {children}
    </div>
  );
}

/** A sheet over the cards: what a rail pane showed in full (a decision, a
 * support ticket, a message from the platform), on a desktop a panel in the
 * middle, on a phone the whole screen. Closes on Escape, its ✕, and a click
 * on the shade around it. */
export function Sheet({
  title,
  children,
  onClose,
  data,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  data?: string;
}) {
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [onClose]);
  return (
    <div
      className="shade"
      data-sheet={data ?? ""}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div aria-label={title} aria-modal="true" className="sheetpanel" role="dialog">
        <div className="sheethead">
          <h3>{title}</h3>
          <button className="btn quiet" data-sheet-close onClick={onClose} type="button">
            Close
          </button>
        </div>
        <div className="sheetbody">{children}</div>
      </div>
    </div>
  );
}
