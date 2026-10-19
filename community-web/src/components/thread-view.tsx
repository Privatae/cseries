// One thread: header + markdown body, then the reply tree. The server returns
// posts flat and parent-linked; buildTree() nests them. Replies compose inline
// (no modal) — the reply box keeps ONE op_id for its lifetime, so a retry of a
// timed-out submit can't double-post.

import { useCallback, useEffect, useState } from "react";
import { ArrowLeft, CornerDownRight, GitPullRequest, Loader2 } from "lucide-react";

import * as api from "@/lib/api";
import { buildTree, type Post, type PostNode, type Thread } from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  Byline,
  FlagButton,
  Markdown,
  StatusChip,
  Unavailable,
  VotePill,
} from "@/components/bits";

function ReplyBox({
  threadId,
  parentId,
  onDone,
  onCancel,
  autoFocus,
}: {
  threadId: number;
  parentId?: number;
  onDone: (p: Post) => void;
  onCancel?: () => void;
  autoFocus?: boolean;
}) {
  const [body, setBody] = useState("");
  const [opId] = useState(api.mintOpId); // stable for this box's lifetime
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    if (!body.trim() || busy) return;
    setBusy(true);
    setErr("");
    const r = await api.createReply({
      op_id: opId,
      thread_id: threadId,
      parent_id: parentId ?? null,
      body,
    });
    setBusy(false);
    if (r.ok) {
      setBody("");
      onDone(r.data.post);
    } else {
      setErr(r.message);
    }
  };

  return (
    <div className="mt-2">
      <textarea
        value={body}
        onChange={(e) => setBody(e.target.value)}
        autoFocus={autoFocus}
        rows={3}
        placeholder={parentId ? "Reply…" : "Add to the discussion…"}
        className="w-full resize-y rounded-md border border-input bg-background p-2 text-sm outline-none focus:ring-1 focus:ring-ring"
      />
      {err && <div className="mt-1 text-xs text-destructive">{err}</div>}
      <div className="mt-1 flex items-center gap-2">
        <button
          onClick={() => void submit()}
          disabled={busy || !body.trim()}
          className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1 text-xs font-medium text-primary-foreground disabled:opacity-50 hover:opacity-90"
        >
          {busy && <Loader2 size={12} className="animate-spin" />} Reply
        </button>
        {onCancel && (
          <button onClick={onCancel} className="text-xs text-muted-foreground hover:underline">
            cancel
          </button>
        )}
      </div>
    </div>
  );
}

function PostItem({
  node,
  threadId,
  locked,
  depth,
  onNewReply,
}: {
  node: PostNode;
  threadId: number;
  locked: boolean;
  depth: number;
  onNewReply: (p: Post) => void;
}) {
  const [replying, setReplying] = useState(false);

  return (
    <div className={cn(depth > 0 && "border-l border-border pl-3", depth > 0 && "ml-1")}>
      <div className="py-2">
        <div className="flex items-center gap-2">
          <Byline author={node.author_name} at={node.created_at} />
          <VotePill kind="post" id={node.id} votes={node.votes} compact />
          <FlagButton kind="post" id={node.id} />
        </div>
        {node.deleted ? (
          <div className="mt-1 text-xs italic text-muted-foreground">[removed]</div>
        ) : (
          <div className="mt-1">
            <Markdown>{node.body}</Markdown>
          </div>
        )}
        {!locked && !replying && (
          <button
            onClick={() => setReplying(true)}
            className="mt-1 flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground"
          >
            <CornerDownRight size={11} /> reply
          </button>
        )}
        {replying && (
          <ReplyBox
            threadId={threadId}
            parentId={node.id}
            autoFocus
            onCancel={() => setReplying(false)}
            onDone={(p) => {
              setReplying(false);
              onNewReply(p);
            }}
          />
        )}
      </div>
      {node.children.map((c) => (
        <PostItem
          key={c.id}
          node={c}
          threadId={threadId}
          locked={locked}
          depth={depth + 1}
          onNewReply={onNewReply}
        />
      ))}
    </div>
  );
}

/** The ladder button — escalate this thread into the governed proposal
 * queue. Two-step confirm (it opens a real PR); once stamped, the button is
 * gone for good and the "proposed #N" chip takes over. */
function ProposeButton({
  threadId,
  onProposed,
}: {
  threadId: number;
  onProposed: (ref: number) => void;
}) {
  const [state, setState] = useState<"idle" | "confirm" | "busy">("idle");
  const [err, setErr] = useState("");

  if (state === "confirm" || state === "busy") {
    return (
      <span className="inline-flex items-center gap-2 text-[11px]">
        <span className="text-muted-foreground">
          Open a governed proposal from this thread (votes ride along)?
        </span>
        <button
          disabled={state === "busy"}
          onClick={async () => {
            setState("busy");
            setErr("");
            const r = await api.propose(threadId);
            if (r.ok) onProposed(r.data.proposal_ref);
            else {
              setErr(r.message);
              setState("confirm");
            }
          }}
          className="rounded-md bg-primary px-2 py-0.5 text-[11px] font-medium text-primary-foreground disabled:opacity-50"
        >
          {state === "busy" ? "proposing…" : "yes, propose"}
        </button>
        <button onClick={() => setState("idle")} className="text-muted-foreground hover:underline">
          cancel
        </button>
        {err && <span className="text-destructive">{err}</span>}
      </span>
    );
  }
  return (
    <button
      onClick={() => setState("confirm")}
      title="Escalate into the governed proposal queue"
      className="inline-flex items-center gap-1 rounded-md border border-border px-1.5 py-0.5 text-[11px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
    >
      <GitPullRequest size={11} /> Propose this
    </button>
  );
}

export function ThreadView({ id, onBack }: { id: number; onBack: () => void }) {
  const [thread, setThread] = useState<Thread | null>(null);
  const [posts, setPosts] = useState<Post[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    const r = await api.fetchThread(id);
    setLoading(false);
    if (r.ok) {
      setThread(r.data.thread);
      setPosts(r.data.posts);
    } else {
      setError(r.message);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading && !thread) {
    return (
      <div className="flex h-full items-center justify-center">
        <Loader2 className="animate-spin text-muted-foreground" size={20} />
      </div>
    );
  }
  if (error && !thread) return <Unavailable message={error} retry={() => void load()} />;
  if (!thread) return null;

  const locked = thread.status === "locked";
  const tree = buildTree(posts);

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2">
        <button
          onClick={onBack}
          className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft size={14} /> back
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-base font-semibold text-foreground">{thread.title}</h2>
          <StatusChip status={thread.status} proposalRef={thread.proposal_ref} />
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <Byline author={thread.author_name} at={thread.created_at} />
          <VotePill kind="thread" id={thread.id} votes={thread.votes} />
          <FlagButton kind="thread" id={thread.id} />
          {!thread.proposal_ref && !locked && (
            <ProposeButton
              threadId={thread.id}
              onProposed={(ref) => setThread({ ...thread, proposal_ref: ref })}
            />
          )}
        </div>
        <div className="mt-3 border-b border-border pb-4">
          <Markdown>{thread.body}</Markdown>
        </div>

        {!locked && (
          <ReplyBox threadId={thread.id} onDone={(p) => setPosts((ps) => [...ps, p])} />
        )}
        {locked && (
          <div className="mt-3 text-xs text-muted-foreground">
            This thread is locked — replies are off.
          </div>
        )}

        <div className="mt-2">
          {tree.map((n) => (
            <PostItem
              key={n.id}
              node={n}
              threadId={thread.id}
              locked={locked}
              depth={0}
              onNewReply={(p) => setPosts((ps) => [...ps, p])}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
