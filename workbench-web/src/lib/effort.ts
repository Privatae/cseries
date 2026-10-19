// The per-thread reasoning-effort pick in the project thread's header.
//
// The rule is the server's, never this file's: GET /api/inference/effort-options
// says which levels the Host model route's model takes and the level that runs
// when none is picked, and the send route (/api/brain/messages) checks the pick
// again by the same rule (server/routes/inference.py check_thread_effort). This
// file only words the options and keeps a pick honest when the model under it
// changes.

/** GET /api/inference/effort-options (server/routes/inference.py
 * effort_options): the kit's, which the book's hold-to-think reads too. */
import type { EffortOptions } from "@kit/effort";
export type { EffortOptions };

/** The blank pick, read as what runs: "Default · medium", or "Default" when
 * the model's own level is not named. Never an empty label. */
export function defaultLabel(options: EffortOptions | null): string {
  return options?.default ? `Default · ${options.default}` : "Default";
}

/** The pick, if the model now under it still takes it; else "" (its
 * default). A model change must not carry a level the new model refuses. */
export function keepPick(pick: string, options: EffortOptions | null): string {
  return pick && options && options.efforts.includes(pick) ? pick : "";
}

/** Whether there is anything to choose for this model. */
export function hasChoices(options: EffortOptions | null): boolean {
  return !!options && options.efforts.length > 0;
}
