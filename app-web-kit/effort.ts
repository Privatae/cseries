// The reasoning-effort options a Host model takes, as the server words them.
// Both apps read the same route; the rule is the server's, never this file's
// (server/routes/inference.py check_thread_effort).

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
