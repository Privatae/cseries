// The per-thread reasoning-effort pick beside the model picker.
//
// The rule is the server's, never this file's: GET /api/inference/effort-options
// says which levels the model a turn runs on takes (the picked model, else the
// Host model route) and the level that runs when none is picked, and the send
// route checks the pick again by the same rule (server/routes/inference.py
// check_thread_effort). This file only words the options and keeps a pick
// honest when the model under it changes.

/** GET /api/inference/effort-options (server/routes/inference.py effort_options). */
export interface EffortOptions {
  provider: string;
  model: string;
  /** The levels a pick may be, low to high; [] = the model takes none. */
  efforts: string[];
  /** The level that runs when none is picked, when the docs name it. */
  default: string | null;
  /** False when the model list could not say (efforts is then []). */
  known: boolean;
}

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
