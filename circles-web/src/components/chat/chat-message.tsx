import {
  Message,
  MessageContent,
  MessageResponse,
} from "@/components/ai-elements/message";
import { TriangleAlertIcon } from "lucide-react";
import { memo, useEffect, useRef, useState } from "react";
import { ErrorBoundary } from "@/components/error-boundary";
import { ToolCard } from "./tool-card";
import { ArtifactCard, artifactIdOf, artifactsOfTurn, isOwnHostTurn, VisualsAllowed } from "@kit/visuals";
import type { ChatMessage } from "@/lib/types";
import { failedOverNote as failedOverNoteFor, metricsBits } from "@/lib/metrics-format";
import { CloseTurnReview, parseCloseTurn } from "@kit/close-turn";

/** The host's live thinking, streamed into the bubble as it happens (`g:`
 * frames). Auto-open while the host reasons, auto-collapse to a one-line
 * header once the reply starts; the user can toggle either way. Live-only —
 * persisted history doesn't carry reasoning, so this renders nothing there. */
function ReasoningFeed({ text, live }: { text: string; live: boolean }) {
  const [userOpen, setUserOpen] = useState<boolean | null>(null); // null = auto
  const open = userOpen ?? live;
  const boxRef = useRef<HTMLDivElement | null>(null);
  // Follow the live feed: keep the newest thinking in view while streaming.
  useEffect(() => {
    if (open && live && boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }
  }, [text, open, live]);
  return (
    <div className="mb-1">
      <button
        className="flex items-center gap-1.5 text-muted-foreground text-xs hover:text-foreground"
        onClick={() => setUserOpen(!open)}
        type="button"
      >
        <span aria-hidden>∴</span>
        <span>{live ? "Reasoning…" : "Reasoned"}</span>
        <span aria-hidden className="text-[10px]">
          {open ? "▾" : "▸"}
        </span>
      </button>
      {open && (
        <div
          className="mt-1 max-h-48 overflow-y-auto whitespace-pre-wrap rounded-md border border-border border-dashed bg-muted/30 px-3 py-2 text-muted-foreground text-xs"
          ref={boxRef}
        >
          {text}
        </div>
      )}
    </div>
  );
}

function fmtClock(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime()) || d.getTime() === 0) return "";
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return sameDay
    ? time
    : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

/** The quiet meta line under a turn: time · who answered · took Ns · N tok ·
 * $price. The bits come from lib/metrics-format so a regenerated alternate
 * and each A/B side print the same line and read as a price comparison. */
function TurnMeta({ message }: { message: ChatMessage }) {
  const clock = fmtClock(message.createdAt);
  const mt = message.metrics;
  const bits: string[] = [];
  if (clock) bits.push(clock);
  let failedOverNote: string | null = null;
  if (message.role === "assistant" && mt) {
    bits.push(...metricsBits(mt));
    failedOverNote = failedOverNoteFor(mt);
  }
  const review = message.role === "assistant" ? parseCloseTurn(mt?.close_turn) : null;
  const [showReview, setShowReview] = useState(false);
  if (bits.length === 0) return null;
  return (
    <div
      className={
        message.role === "user"
          ? "mt-0.5 text-right text-[11px] text-muted-foreground/70"
          : "mt-0.5 text-left text-[11px] text-muted-foreground/70"
      }
    >
      {bits.join(" · ")}
      {failedOverNote && (
        <span className="ml-1 text-amber-600 dark:text-amber-400" title={failedOverNote}>
          · {failedOverNote.length > 90 ? failedOverNote.slice(0, 90) + "…" : failedOverNote}
        </span>
      )}
      {review && (
        <>
          {" · "}
          <button
            type="button"
            className="underline decoration-dotted underline-offset-2 hover:text-foreground"
            onClick={() => setShowReview((v) => !v)}
            aria-expanded={showReview}
          >
            {showReview ? "hide review" : "review"}
          </button>
          {showReview && <CloseTurnReview s={review} />}
        </>
      )}
    </div>
  );
}

/** WHO wrote it — printed above a turn in Peers / People threads, where the
 * other side is a named host or a named person and a person's message says
 * which host carried it. The self thread carries no author and prints nothing. */
function AuthorLine({ message }: { message: ChatMessage }) {
  if (!message.author) return null;
  const isPerson = message.authorKind === "person" || message.authorKind === "self";
  return (
    <div
      className={
        message.role === "user"
          ? "mb-0.5 text-right text-[11px] text-muted-foreground"
          : "mb-0.5 text-left text-[11px] text-muted-foreground"
      }
    >
      <span className="font-medium text-foreground/80">{message.author}</span>
      {isPerson ? (
        <span>{message.via ? ` · via ${message.via}` : " · person"}</span>
      ) : (
        <span>
          {" · host"}
          {message.claimedPerson ? ` · says this is from ${message.claimedPerson}` : ""}
        </span>
      )}
    </div>
  );
}

function ErrorInline({ text }: { text: string }) {
  return (
    <div className="mt-1 flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-destructive text-sm">
      <TriangleAlertIcon className="mt-0.5 size-4 shrink-0" />
      <span className="min-w-0 break-words">{text}</span>
    </div>
  );
}

/** Memoized: a streaming frame or composer keystroke re-renders only the
 * message whose props changed. History adoption preserves object identity for
 * unchanged rows (history-merge), so the memo holds across refetches. */
export const ChatMessageView = memo(function ChatMessageView({
  message,
}: {
  message: ChatMessage;
}) {
  if (message.role === "user") {
    return (
      <Message from="user">
        <MessageContent>
          <AuthorLine message={message} />
          <span className="whitespace-pre-wrap">{message.text}</span>
          <TurnMeta message={message} />
        </MessageContent>
      </Message>
    );
  }

  // Turn finished but produced no visible reply and no explicit error — e.g.
  // every tool failed, or a persisted turn row with an empty body. Render an
  // explicit note instead of a silent bubble (the never-silent invariant; the
  // hook's onDone only covers the live zero-tool case, not history or
  // tools-ran-but-no-text turns).
  const finishedEmpty = !message.streaming && !message.text && !message.error;

  // The user's own Host wrote this. Only then do its visuals run,
  // and only then do its artifact ids name artifacts in THIS Host's store.
  // Marked where the message was built (self-thread rows, the live stream);
  // author/authorKind cannot tell — a self-thread reply arrives as
  // author_kind "entity", exactly like a peer host's.
  const ownHost = isOwnHostTurn(message);
  // The artifacts this turn made or changed, each shown once as the page.
  // rev = how many calls have finished on it, so it redraws as writes land.
  const artifacts = ownHost
    ? artifactsOfTurn(message.tools).map((id) => ({
        id,
        rev: message.tools.filter((t) => t.state === "output-available" && artifactIdOf(t) === id).length,
      }))
    : [];

  return (
    <VisualsAllowed.Provider value={ownHost}>
    <Message from="assistant">
      <MessageContent>
        <AuthorLine message={message} />
        {message.agent && (
          // Delivered by a background agent — say so, or this turn is
          // indistinguishable from the host just talking (spec "Agent UI").
          <div className="mb-1 flex items-center gap-1.5 text-muted-foreground text-xs">
            <span aria-hidden>⚙</span>
            <span>
              Background agent
              {message.agent.taskId ? ` #${message.agent.taskId}` : ""}
              {message.agent.title ? ` — ${message.agent.title}` : ""}
              {message.agent.status && message.agent.status !== "completed"
                ? ` · ${message.agent.status}`
                : ""}
            </span>
          </div>
        )}
        {message.reasoning && (
          <ReasoningFeed
            live={!!message.streaming && !message.text}
            text={message.reasoning}
          />
        )}
        {message.tools.length > 0 && (
          <div className="flex flex-col">
            {message.tools.map((t) => (
              // One malformed tool payload must cost one card, not the app.
              <ErrorBoundary key={t.id} label={`tool:${t.name}`}>
                <ToolCard
                  defaultOpen={t.state === "output-error"}
                  tool={t}
                />
              </ErrorBoundary>
            ))}
          </div>
        )}
        {artifacts.map((a) => (
          <ErrorBoundary key={`artifact-${a.id}`} label={`artifact:${a.id}`}>
            <ArtifactCard id={a.id} rev={a.rev} />
          </ErrorBoundary>
        ))}
        {message.text && (
          // Markdown/Shiki can throw on partial mid-stream input — fall back
          // to the same text as plain prose, never a blank bubble.
          <ErrorBoundary
            fallback={() => (
              <div className="whitespace-pre-wrap text-foreground text-sm">
                {message.text}
              </div>
            )}
            label="markdown"
          >
            {/* isAnimating is what makes Streamdown flag a fence that is
                still open (its isIncomplete is computed only while
                animating), so a visual is not run half-written. */}
            <MessageResponse isAnimating={!!message.streaming}>{message.text}</MessageResponse>
          </ErrorBoundary>
        )}
        {/* A running turn's step and clock are on the one working line above
            the box (cards/activity.tsx), not repeated under the reply. */}
        {!message.streaming && <TurnMeta message={message} />}
        {message.error && <ErrorInline text={message.error} />}
        {finishedEmpty && (
          <div className="text-muted-foreground text-sm italic">
            Finished without a reply
            {message.tools.some((t) => t.state === "output-error")
              ? " — a tool call failed. Try again."
              : " — try again."}
          </div>
        )}

      </MessageContent>
    </Message>
    </VisualsAllowed.Provider>
  );
});
