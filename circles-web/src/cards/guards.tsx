import type { ReactNode } from "react";
import { MessageResponse } from "@/components/ai-elements/message";
import { ErrorBoundary } from "@/components/error-boundary";

// The rail caught a crash in any pane ("This pane hit a rendering error",
// with Retry: App.tsx on 008187f1e) and wrapped every message, drawing it as
// plain text when the markdown renderer failed (chat-view.tsx and
// chat-message.tsx there). The cards do the same: a crash on the deck, a
// card's face, a room or a sheet stays inside it, and one message that breaks
// the renderer is shown as its words.

/** A part of Circles that shows a contained error with Retry if it crashes,
 * never a blank window. `label` names it in the console and the page. */
export function Guard({
  label,
  children,
  onClose,
}: {
  label: string;
  children: ReactNode;
  /** A sheet's way out, offered beside Retry. */
  onClose?: () => void;
}) {
  return (
    <ErrorBoundary
      fallback={(reset) => (
        <div className="blank" data-render-error={label} role="alert">
          This part of Circles hit a rendering error.{" "}
          <button
            className="link"
            data-render-retry
            onClick={reset}
            type="button"
          >
            Retry
          </button>
          {onClose ? (
            <>
              {" "}
              <button
                className="link"
                data-render-close
                onClick={onClose}
                type="button"
              >
                Close
              </button>
            </>
          ) : null}
        </div>
      )}
      label={label}
    >
      {children}
    </ErrorBoundary>
  );
}

/** What a message says, as plain text: its words, or for a body that is not
 * text at all, that body written out. */
export function plainOf(body: unknown): string {
  if (typeof body === "string") return body;
  if (body == null) return "";
  try {
    return JSON.stringify(body);
  } catch {
    return String(body);
  }
}

/** The markdown renderer takes text only. A body that is not text (a
 * malformed row) is refused here, inside the guard, the same way a renderer
 * failure is, so it is shown written out rather than drawn as nothing. */
function Markdown({ text }: { text: unknown }) {
  if (typeof text !== "string") throw new TypeError("a message body that is not text");
  return <MessageResponse>{text}</MessageResponse>;
}

/** A message's words as markdown, or as plain text if the renderer fails on
 * them. */
export function SafeMarkdown({ text }: { text: unknown }) {
  return (
    <ErrorBoundary
      fallback={() => (
        <p data-plain-fallback style={{ whiteSpace: "pre-wrap", margin: 0 }}>
          {plainOf(text)}
        </p>
      )}
      label="message"
    >
      <Markdown text={text} />
    </ErrorBoundary>
  );
}
