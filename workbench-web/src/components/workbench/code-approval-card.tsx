import { useEffect, useRef, useState } from "react";
import { ExternalLinkIcon, FolderGit2Icon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import {
  announceApproval,
  askForChanges,
  fetchApproval,
  fetchApprovalDiff,
} from "@/lib/api";
import {
  openRepository,
  parseApprovalBody,
  splitLinks,
} from "@/lib/code-approval";
import type { CodeApprovalDetail } from "@/lib/types";
import { cn } from "@/lib/utils";

// How long the card watches the forge after Approve opens the platform's
// window: long enough to read the change there, then it stops asking.
const WATCH_MS = 15 * 60 * 1000;
const WATCH_EVERY_MS = 3000;

/**
 * A Code Approval, over the chat: what it changes in plain words, the
 * full change folded under it, and Approve / Ask for changes / Not now.
 *
 * Approve never merges from here. It opens a window on the platform's own
 * site, which reads the change from the forge and merges it with the person's
 * own forge account when they approve there — a page this Host's code cannot
 * script. The card then watches the forge until the merge is on record and
 * asks the Host to write the chat line from that record.
 */
export function CodeApprovalCard({
  projectId,
  number,
  onClose,
  onAskChanges,
  onMerged,
}: {
  projectId: number;
  /** The approval to show; null renders nothing. */
  number: number | null;
  onClose: () => void;
  /** The person's words for the Host, sent as their next message. */
  onAskChanges: (message: string) => void;
  /** The approval merged. `line`: "written" when this card wrote its chat
   * line; "held" when another writer (the bench's list, another tab) holds
   * the write, which may still fail and be retried; "none" when nothing was
   * written. The chat waits for a held line too, quietly: its writer raises a
   * wait of its own, and reports a miss, once the line is written. */
  onMerged: (number: number, line: "written" | "held" | "none") => void;
}) {
  const [detail, setDetail] = useState<CodeApprovalDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [diff, setDiff] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [watching, setWatching] = useState(false);
  const [blockedUrl, setBlockedUrl] = useState<string | null>(null);
  const watchRef = useRef<{ stop: boolean }>({ stop: false });

  useEffect(() => {
    watchRef.current.stop = true;
    setDetail(null);
    setError(null);
    setDiff(null);
    setAsking(false);
    setNote("");
    setWatching(false);
    setBlockedUrl(null);
    if (number == null) return;
    let alive = true;
    (async () => {
      const r = await fetchApproval(projectId, number);
      if (!alive) return;
      if (r.ok) setDetail(r.data);
      else setError(r.message);
    })();
    return () => {
      alive = false;
    };
  }, [projectId, number]);

  useEffect(() => () => {
    watchRef.current.stop = true;
  }, []);

  if (number == null) return null;

  const parsed = parseApprovalBody(detail?.body);
  const open = detail?.state === "waiting" || detail?.state === "changes_asked";

  const loadDiff = async () => {
    if (diff != null) return;
    setDiff("loading…");
    const r = await fetchApprovalDiff(projectId, number);
    setDiff(r.ok ? r.data.diff || "(no changes)" : r.message);
  };

  // Watch the forge for the merge the platform's window makes, until it is on
  // record or WATCH_MS passes. The window itself is never read: the approve
  // page isolates itself from this one (Cross-Origin-Opener-Policy), so its
  // handle reads `closed` a moment after it loads while it is still open.
  // One watch at a time: a second Approve (the window was closed, or never
  // opened) stops the first before it starts, and a stopped watch neither
  // writes into the card nor ends a newer watch. A merge whose chat line was
  // written always refreshes the chat, even if the card was closed or moved
  // to another approval meanwhile. A merge the card stopped watching before
  // it saw is announced by the bench list, which refreshes the chat too.
  const watch = async () => {
    watchRef.current.stop = true;
    const token = { stop: false };
    watchRef.current = token;
    setWatching(true);
    const started = Date.now();
    while (!token.stop && Date.now() - started < WATCH_MS) {
      await new Promise((res) => setTimeout(res, WATCH_EVERY_MS));
      if (token.stop) return;
      const r = await fetchApproval(projectId, number);
      if (token.stop) return;
      if (r.ok) {
        setDetail(r.data);
        if (r.data.state === "approved") {
          const a = await announceApproval(projectId, number);
          if (watchRef.current === token) setWatching(false);
          onMerged(number, !a.ok ? "none" : a.data.announced ? "written" : a.data.already ? "held" : "none");
          return;
        }
      }
    }
    if (!token.stop) setWatching(false);
  };

  const approve = () => {
    if (!detail?.approve_url) return;
    const win = window.open(detail.approve_url, "code-approval", "popup,width=760,height=900");
    setBlockedUrl(win ? null : detail.approve_url);
    void watch();
  };

  const sendChanges = async () => {
    setBusy(true);
    setError(null);
    const r = await askForChanges(projectId, number, note);
    setBusy(false);
    if (!r.ok) {
      setError(r.message);
      return;
    }
    onAskChanges(r.data.message);
    onClose();
  };

  return (
    <Dialog onOpenChange={(o) => { if (!o && !busy) onClose(); }} open>
      <DialogContent aria-describedby="code-approval-summary" className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 pr-8">
            <span className="min-w-0 truncate">
              {detail?.title ?? `Code Approval #${number}`}
            </span>
            {detail && (
              <Badge
                className="shrink-0"
                variant={detail.state === "waiting" ? "default" : "secondary"}
              >
                {detail.state_words}
              </Badge>
            )}
          </DialogTitle>
          <DialogDescription id="code-approval-summary">
            Code Approval #{number}
            {detail?.repo && (
              <>
                {" · "}
                {detail.on_machine ? (
                  <span className="font-mono">{detail.repo}</span>
                ) : (
                  <button
                    className="inline-flex items-center gap-1 font-mono underline-offset-2 hover:underline"
                    onClick={() => openRepository(detail.repo)}
                    title="Open this Code Repository"
                    type="button"
                  >
                    <FolderGit2Icon className="size-3" />
                    {detail.repo}
                  </button>
                )}
              </>
            )}
            {detail?.changed_files != null &&
              ` · ${detail.changed_files} file${detail.changed_files === 1 ? "" : "s"} changed (+${detail.additions ?? 0} −${detail.deletions ?? 0})`}
          </DialogDescription>
        </DialogHeader>

        {!detail && !error && <p className="text-muted-foreground text-sm">Loading…</p>}

        {detail && (
          <div className="max-h-[55vh] space-y-3 overflow-auto pr-1 text-sm">
            {parsed.plain ? (
              parsed.sections.map((s) => (
                <section key={s.heading}>
                  <h3 className="mb-1 font-medium text-muted-foreground text-xs uppercase tracking-wide">
                    {s.heading}
                  </h3>
                  <ul className="space-y-0.5">
                    {s.lines.map((line, i) => (
                      <li
                        className={cn(line.startsWith("  • ") && "pl-4 text-muted-foreground")}
                        key={`${s.heading}-${i}`}
                      >
                        {splitLinks(line.replace(/^ {2}• /, "• ")).map((part, j) =>
                          part.href ? (
                            <a
                              className="text-primary underline-offset-2 hover:underline"
                              href={part.href}
                              key={j}
                              rel="noopener noreferrer"
                              target="_blank"
                            >
                              {part.text}
                            </a>
                          ) : (
                            <span key={j}>{part.text}</span>
                          ),
                        )}
                      </li>
                    ))}
                  </ul>
                </section>
              ))
            ) : (
              <pre className="whitespace-pre-wrap font-sans text-sm">
                {parsed.text || "No description."}
              </pre>
            )}
            {parsed.details.length > 0 && (
              <ul className="space-y-0.5 text-muted-foreground text-xs">
                {parsed.details.map((d, i) => (
                  <li key={i}>{d}</li>
                ))}
              </ul>
            )}
            <details
              className="rounded-md border border-border"
              onToggle={(e) => {
                if ((e.target as HTMLDetailsElement).open) void loadDiff();
              }}
            >
              <summary className="cursor-pointer px-3 py-2 text-muted-foreground text-xs">
                The full change, for anyone who wants to read the code
              </summary>
              <pre className="max-h-80 overflow-auto whitespace-pre border-border border-t bg-background p-3 font-mono text-xs">
                {(diff ?? "").split("\n").map((line, i) => (
                  <span
                    className={cn(
                      "block",
                      line.startsWith("+") && !line.startsWith("+++")
                        ? "bg-success/10 text-success"
                        : line.startsWith("-") && !line.startsWith("---")
                          ? "bg-destructive/10 text-destructive"
                          : line.startsWith("@@")
                            ? "text-muted-foreground"
                            : "",
                    )}
                    key={`${i}-${line.slice(0, 8)}`}
                  >
                    {line || " "}
                  </span>
                ))}
              </pre>
            </details>
          </div>
        )}

        {detail && open && !detail.ready && (
          <p className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs">
            The build contract found problems with this change. Approving it puts code that
            failed its checks into {detail.base ? `\`${detail.base}\`` : "the main line"}.
          </p>
        )}
        {detail?.state === "approved" && (
          <p className="text-sm">
            Approved{detail.merged_by ? ` by ${detail.merged_by}` : ""}
            {detail.merged_at ? ` at ${new Date(detail.merged_at).toLocaleString()}` : ""}.
            {detail.base ? ` It is in ${detail.base}.` : ""}
          </p>
        )}
        {detail && open && detail.approve_where && (
          <p className="text-muted-foreground text-xs">{detail.approve_where}</p>
        )}
        {watching && (
          <p className="text-muted-foreground text-xs" role="status">
            Approve it in the privatae.ai window that opened. This card updates when it is
            done.
          </p>
        )}
        {blockedUrl && (
          <p className="text-xs">
            Your browser blocked the window.{" "}
            <a
              className="inline-flex items-center gap-1 text-primary underline-offset-2 hover:underline"
              href={blockedUrl}
              rel="noopener noreferrer"
              target="_blank"
            >
              Open it here <ExternalLinkIcon className="size-3" />
            </a>
          </p>
        )}
        {asking && (
          <label className="block text-sm">
            <span className="text-muted-foreground text-xs">What should change?</span>
            <Textarea
              autoFocus
              className="mt-1 min-h-20"
              disabled={busy}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Say it the way you would to a person"
              value={note}
            />
          </label>
        )}
        {error && (
          <div
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-destructive text-xs"
            role="alert"
          >
            {error}
          </div>
        )}

        <DialogFooter className="gap-2 sm:justify-end">
          {detail && open && !asking && (
            <>
              <Button disabled={busy} onClick={onClose} type="button" variant="ghost">
                Not now
              </Button>
              <Button disabled={busy} onClick={() => setAsking(true)} type="button" variant="outline">
                Ask for changes
              </Button>
              {detail.approve_url && (
                <Button disabled={busy} onClick={approve} type="button">
                  {watching ? "Open the approval window again" : "Approve"}
                </Button>
              )}
            </>
          )}
          {asking && (
            <>
              <Button disabled={busy} onClick={() => setAsking(false)} type="button" variant="ghost">
                Back
              </Button>
              <Button disabled={busy || !note.trim()} onClick={sendChanges} type="button">
                Send to your Host
              </Button>
            </>
          )}
          {detail && !open && (
            <Button onClick={onClose} type="button" variant="ghost">
              Close
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
