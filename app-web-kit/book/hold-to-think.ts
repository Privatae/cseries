// Hold-to-think on the book's Send: a tap sends at
// the page's effort; holding climbs to high, then max, showing the level as it
// climbs, and letting go sends at the level reached. Only levels the page's
// model takes are offered (GET /api/inference/effort-options, the same list
// the page's effort pick shows). The send carries a per-message effort, which
// the server checks against the Host model and applies to that one turn (the
// Workbench's POST /api/brain/messages, server/routes/brain.py). Pure, so
// workbench-web/test/hold-to-think.test.ts pins it.

import type { EffortOptions } from "../effort.ts";

/** The levels a hold climbs to, in order. */
export const CLIMB = ["high", "max"] as const;

/** A press shorter than this is a tap. Half a second is the long-press of
 * the platforms a phone runs: iOS's long-press recognizer defaults to 0.5 s,
 * Android's long-press timeout is 400 to 500 ms. So a hold on a phone feels
 * like every other long press, and a quick click or Enter is never one. */
export const HOLD_FIRST_MS = 500;

/** Each further level after the first: long enough to let go on the level
 * you want, short enough that max is under a second and a quarter away. */
export const HOLD_STEP_MS = 700;

/** The press shows it is being held from here on (a circle filling toward
 * the first level), so a tap never flickers. */
export const HOLD_SHOW_MS = 150;

/** What each level means, in the page's words (the mockup's). */
export const LEVEL_WORDS: Record<string, string> = {
  minimal: "quickest",
  low: "quick",
  medium: "balanced",
  high: "thinks longer",
  xhigh: "thinks longer still",
  max: "thinks hardest, costs most",
};

/** The page's effort: the page's own pick, else the level the model runs at
 * when none is picked, when that is known; else null. */
export function pageEffort(pick: string | null | undefined, options: EffortOptions | null | undefined): string | null {
  return (pick || "").trim() || options?.default || null;
}

/** The levels a hold climbs through on this page: high, then max, each only
 * when the model takes it and it is above the page's effort. With the page's
 * effort unknown, every one the model takes. None for a model that takes no
 * effort. */
export function climbLevels(options: EffortOptions | null | undefined, page: string | null): string[] {
  const levels = options?.efforts ?? [];
  if (!levels.length) return [];
  const at = page == null ? -1 : levels.indexOf(page);
  return CLIMB.filter((lv) => levels.includes(lv) && levels.indexOf(lv) > at);
}

/** The level a press held `ms` has reached, or null while it is still a tap
 * (or there is nothing to climb to). */
export function levelAt(ms: number, climb: readonly string[]): string | null {
  if (!climb.length || ms < HOLD_FIRST_MS) return null;
  return climb[Math.min(climb.length - 1, Math.floor((ms - HOLD_FIRST_MS) / HOLD_STEP_MS))];
}

/** How far through the climb a press held `ms` is, 0 to 1: the circle that
 * fills round the press. */
export function holdProgress(ms: number, climb: readonly string[]): number {
  if (!climb.length) return 0;
  const top = HOLD_FIRST_MS + HOLD_STEP_MS * (climb.length - 1);
  return Math.max(0, Math.min(1, ms / top));
}

/** The effort a press sends with: the level reached, else (a tap) the page's
 * own pick, else nothing, so the model's default runs as before. */
export function sendEffort(reached: string | null, pick: string | null | undefined): string | null {
  return reached || (pick || "").trim() || null;
}

/** The line under the composer. */
export function composerHint(opts: {
  nudging: boolean;
  page: string | null;
  climb: readonly string[];
}): string {
  if (opts.nudging) {
    return "Enter nudges: your words reach the Host before its next step, without stopping it.";
  }
  const at = opts.page ? `Enter sends at ${opts.page}` : "Enter sends";
  return opts.climb.length
    ? `${at} · hold Enter or the button to think harder (${opts.climb.join(", then ")})`
    : at;
}
