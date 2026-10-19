import type { ChatMessage } from "@/lib/types";

/** Friendly step labels for the tools the host commonly runs; anything else
 * falls back to "Running <name>". */
const TOOL_LABELS: Record<string, string> = {
  memory_recall: "Searching memory",
  memory_search: "Searching memory",
  recall: "Searching memory",
  web_search: "Searching the web",
  search: "Searching the web",
  read: "Reading",
  read_file: "Reading files",
  write_file: "Editing files",
  apply_patch: "Editing files",
  create_project: "Creating a project",
  deploy_policy: "Updating policy",
  dispatch: "Dispatching an agent",
  artifact: "Building an artifact",
  // The kernel's hold (lib/holding.ts): the turn waits for a build, an agent
  // or an answer it started.
  waiting: "Waiting on work it started",
};

export function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? `Running ${name.replaceAll("_", " ")}`;
}

/** How long after the last word arrived the step still says "Writing". The
 * runtime's words come in bursts well under a second apart; a longer silence
 * is the model working on something else (a tool, its next step). */
export const WRITING_MS = 2500;

/**
 * What the running turn is doing right now, in plain words, from what its
 * stream has carried: the newest tool still running (results resolve out of
 * order when the Host runs tools in parallel), "Writing" while words are
 * arriving, and "Thinking" otherwise — before the first frame and in every
 * silence between frames.
 *
 * The step used to read "Writing the reply" whenever the turn had any words
 * at all (`message.text ? …`). A message's text is the turn's whole text so
 * far, so a single opening sentence kept that label up through every later
 * tool result and silence, and "Thinking" could only ever show before a
 * turn's first word, so "Writing a reply" was often wrong.
 * Now the step follows when words last ARRIVED (`lastWordsAt`), not whether
 * any ever did.
 */
export function turnStep(message: ChatMessage | null, lastWordsAt: number, now: number): string {
  if (!message) return "Thinking";
  const activeTool = [...message.tools].reverse().find((t) => t.state === "input-available");
  if (activeTool) return toolLabel(activeTool.name);
  if (lastWordsAt > 0 && now - lastWordsAt < WRITING_MS) return "Writing";
  return "Thinking";
}

/** "8s", "2m 05s", "1h 03m": how long the work has run. */
export function elapsedWords(secs: number): string {
  const s = Math.max(0, Math.floor(secs));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}
