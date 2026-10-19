// What the platform's crisis notice remembers on this device (ui/crisis-notice.tsx).
//
// The notice comes up on the turn's `2:[{type:"governance",kind:"crisis"}]`
// frame. It stays until the person dismisses it: a reload before then shows it
// again, and a turn whose frames are replayed (a reattach, a second tab) does
// not raise a notice already dismissed for that turn. Kept in localStorage as
// one JSON value; nothing about the message is stored, only the turn's key.

export const CRISIS_NOTICE_KEY = "platform.crisisNotice";
/** How many dismissed turn keys are kept (the newest). */
export const CRISIS_NOTICE_KEEP = 20;

export interface CrisisNoticeState {
  /** The turn whose notice is up and not yet dismissed, or null. */
  pending: string | null;
  /** Turns whose notice the person dismissed, newest last. */
  dismissed: string[];
}

export function parseCrisisNotice(raw: string | null | undefined): CrisisNoticeState {
  try {
    const v = raw ? JSON.parse(raw) : null;
    if (!v || typeof v !== "object") return { pending: null, dismissed: [] };
    const pending = typeof v.pending === "string" && v.pending ? v.pending : null;
    const dismissed = Array.isArray(v.dismissed)
      ? v.dismissed.filter((k: unknown): k is string => typeof k === "string" && !!k).slice(-CRISIS_NOTICE_KEEP)
      : [];
    return { pending, dismissed };
  } catch {
    return { pending: null, dismissed: [] };
  }
}

/** The frame arrived for turn `key`: up, unless that turn's was dismissed. */
export function raiseCrisisNotice(state: CrisisNoticeState, key: string): CrisisNoticeState {
  if (state.dismissed.includes(key)) return state;
  return { ...state, pending: key };
}

/** The person dismissed it: down, and that turn's frames never raise it again. */
export function dismissCrisisNotice(state: CrisisNoticeState): CrisisNoticeState {
  if (!state.pending) return state;
  const dismissed = [...state.dismissed.filter((k) => k !== state.pending), state.pending].slice(
    -CRISIS_NOTICE_KEEP,
  );
  return { pending: null, dismissed };
}

/** A governance frame item that asks for the crisis notice. */
export function isCrisisFrame(item: unknown): boolean {
  const it = item as { type?: unknown; kind?: unknown } | null;
  return !!it && it.type === "governance" && it.kind === "crisis";
}
