import { ConversationDepth } from "@/components/workbench/conversation-depth";
import { EffortPicker } from "@/components/workbench/effort-picker";
import { hasChoices, type EffortOptions } from "@/lib/effort";
import type { ContextFrame } from "@/lib/stream";
import type { ChatMessage } from "@/lib/types";

/**
 * "This page", at the top of the book's right side (src/book/): the model the
 * page's turns run on, shown and never chosen here (the Orchestrator chooses
 * it); the thread's effort pick; and the context and budget panel.
 * The chat pane renders this into the
 * book's slot, because the effort pick and the context frame are its thread's
 * state (hooks/use-workbench-chat.ts) and the book must not hold a second
 * chat client to reach them.
 */
export function PageControls({
  options,
  effort,
  onEffort,
  frame,
  messages,
  projectId,
}: {
  /** GET /api/inference/effort-options: the Host model route's model and the
   * levels it takes (useEffortOptions). */
  options: EffortOptions | null;
  effort: string;
  onEffort: (v: string) => void;
  frame: ContextFrame | null;
  messages: ChatMessage[];
  projectId: number | null;
}) {
  return (
    <div className="book-controls" data-page-controls>
      <div className="book-ctl" data-ctl="model">
        <span>Model</span>
        <span
          className="book-pill"
          title="The Host model. It is chosen in the Orchestrator, for every page."
        >
          {options?.model || "not known yet"}
        </span>
      </div>
      {hasChoices(options) && (
        <div className="book-ctl" data-ctl="effort">
          <EffortPicker onChange={onEffort} options={options} value={effort} />
        </div>
      )}
      <div className="book-ctl" data-ctl="context">
        <span>Context</span>
        <ConversationDepth frame={frame} messages={messages} projectId={projectId} />
      </div>
    </div>
  );
}
