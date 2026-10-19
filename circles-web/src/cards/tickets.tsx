import { useCallback, useEffect, useState } from "react";
import { SendIcon, XIcon } from "lucide-react";
import {
  ErrorState,
  FilterSelect,
  formatTime,
  LoadingLine,
  MainHeader,
  MainScroll,
  Pill,
  type PillTone,
  titleizeToken,
} from "@/components/layout";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  closeSupport,
  decideTicket,
  discardSupport,
  dismissTicket,
  getSupport,
  getTicket,
  listTickets,
  openSupport,
  replySupport,
  sendSupport,
  setSupportShared,
  storeVaultSecret,
} from "@/lib/api";
import { sendRevision } from "@/lib/tickets-view";
import type {
  PhaseLast,
  SharedItem,
  SupportCategory,
  SupportTicket,
  Ticket,
  TicketOption,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { sealErrorText, sealedNote, stepupAsk, stepupCodeReady } from "@kit/ask";
import { Sheet } from "./bits";

// The Tickets pane's cards, moved here whole when the rail went: a decision in full
// (its context, the change an op would make on your machine and what became
// of it, the build phase it gates, who asked and how urgent, "and similar
// going forward", Dismiss), a support ticket in full (the conversation with
// the platform's team, your reply, what was shared and each item's content,
// the change link, Discard and Close), and a new support ticket. The cards
// open them: the Decisions sheet below, and the Support card.


export const DECISION_FILTERS = [
  { value: "open", label: "Open" },
  { value: "decided", label: "Decided" },
  { value: "auto_resolved", label: "Auto-resolved" },
  { value: "dismissed", label: "Dismissed" },
  { value: "", label: "All" },
];

export const SUPPORT_FILTERS = [
  { value: "", label: "Active" },
  { value: "draft", label: "Waiting for your approval" },
  { value: "sent", label: "With the platform" },
  { value: "closed", label: "Closed" },
  { value: "discarded", label: "Discarded" },
];

const CATEGORIES: { value: SupportCategory; label: string }[] = [
  { value: "bug", label: "Something is broken" },
  { value: "question", label: "A question" },
  { value: "feature", label: "It should be able to…" },
  { value: "billing", label: "Billing" },
  { value: "access", label: "Access" },
];

function priorityTone(p: number): PillTone {
  if (p >= 70) return "danger";
  if (p >= 40) return "warning";
  return "neutral";
}

function requesterName(t: Ticket): string {
  return (
    t.requester?.display_name || t.requester?.handle || t.requester?.kind || "—"
  );
}

// The platform's status, said the way the user would say it.
function platformStatusLabel(t: SupportTicket): { text: string; tone: PillTone } {
  if (t.status === "draft") return { text: "waiting for your approval", tone: "warning" };
  if (t.status === "discarded") return { text: "discarded", tone: "neutral" };
  switch (t.platform_status) {
    case "received":
      return { text: "received by the platform", tone: "accent" };
    case "validated":
      // Bugs only: the read-only validator passed the report and
      // it is in the platform's validated cache for the operator Host.
      return { text: "validated, in the operator's queue", tone: "accent" };
    case "reproducing":
      return { text: "being reproduced", tone: "accent" };
    case "pr_open":
      return { text: "a fix is proposed, under review", tone: "accent" };
    case "needs_info":
      return { text: "the platform asked you a question", tone: "warning" };
    case "approved":
      return { text: "fix approved, shipping", tone: "success" };
    case "fixed":
      return { text: "fixed", tone: "success" };
    case "rejected":
      return { text: "declined", tone: "danger" };
    default:
      return { text: t.status === "closed" ? "closed" : "sent", tone: "neutral" };
  }
}


export function supportMark(t: Ticket): number | null {
  const md = (t.metadata && typeof t.metadata === "object" ? t.metadata : {}) as Record<
    string,
    unknown
  >;
  if (md.kind !== "support") return null;
  return typeof md.platform_event_max === "number" ? md.platform_event_max : 0;
}


// The approval card for an op the Host wrote, on one of the person's own
// machines: the machine's own dry-run
// diff, where the click is bound, what became of the click, and every run a
// "from now on" answer has covered since. Read from the kind + payload only
// the pod's own op door writes (the POST route strips both).
interface OpAskRun {
  run?: number;
  params?: unknown;
  state?: string;
  verify_error?: string;
  error?: string;
  at?: string;
}
interface OpAsk {
  server: string;
  changes: string;
  binding: string;
  runs: OpAskRun[];
  result: Record<string, unknown> | null;
}
function opAsk(t: Ticket): OpAsk | null {
  const md = (t.metadata && typeof t.metadata === "object" ? t.metadata : {}) as Record<
    string,
    unknown
  >;
  if (md.kind !== "op_ask") return null;
  const a = md.op_ask;
  if (!a || typeof a !== "object" || Array.isArray(a)) return null;
  const ask = a as Record<string, unknown>;
  const runs = Array.isArray(ask.runs)
    ? (ask.runs.filter((r) => r && typeof r === "object") as OpAskRun[])
    : [];
  const res = md.op_ask_result;
  return {
    server: typeof ask.server === "string" ? ask.server : "",
    changes: typeof ask.changes === "string" ? ask.changes : "",
    binding: typeof ask.binding === "string" ? ask.binding : "",
    runs,
    result: res && typeof res === "object" && !Array.isArray(res) ? (res as Record<string, unknown>) : null,
  };
}

function opAskOutcome(op: OpAsk): string | null {
  const r = op.result;
  if (!r) return null;
  if (r.filed === true && r.approved === true) {
    return "Approved on your device — your Host applies it on its next call.";
  }
  if (r.filed === true) {
    // `binding` already names the device ("one tap on your device (laptop)").
    const where = op.binding || (r.device ? `your approval on ${String(r.device)}` : "your approval");
    return `Your answer is waiting for ${where}. Nothing runs until then.`;
  }
  if (typeof r.why === "string" && r.why) {
    return `Your answer was recorded, but the machine could not be asked: ${r.why}. Your Host asks again when you say so.`;
  }
  return null;
}

function phaseLink(t: Ticket): { phaseId: number | null; last: PhaseLast | null; gate: boolean } {
  const md = (t.metadata && typeof t.metadata === "object" ? t.metadata : {}) as Record<
    string,
    unknown
  >;
  const phaseId = typeof md.phase_id === "number" ? md.phase_id : null;
  const rawLast = md.phase_last;
  const last =
    rawLast && typeof rawLast === "object" && typeof (rawLast as PhaseLast).status === "string"
      ? (rawLast as PhaseLast)
      : null;
  return { phaseId, last, gate: md.kind === "phase_gate" };
}

export function TicketDetail({
  ticket,
  andSimilar,
  decideError = null,
  onDecide,
  onDismiss,
  onToggleSimilar,
}: {
  ticket: Ticket;
  andSimilar: boolean;
  decideError?: string | null;
  onDecide: (optionId: string, note?: string, stepupCode?: string | null) => void;
  onDismiss: () => void;
  onToggleSimilar: (v: boolean) => void;
}) {
  const decidable = ticket.status === "open";
  // The place to write: what the person did, or why not. Sent with whichever
  // option they pick. Reset when the ticket changes.
  const [note, setNote] = useState("");
  const [code, setCode] = useState("");
  // The option that takes a code from the person's authenticator, if any
  // (lib/ticket-stepup.ts: a pipeline check, or a messaging connector's
  // pairing card).
  const stepup = stepupAsk(ticket.metadata);
  const codeFor = stepup?.option ?? null;
  // One sealed field per option that asks for a credential; the value never
  // leaves this component except into the vault.
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [sealing, setSealing] = useState<string | null>(null);
  const [sealError, setSealError] = useState<string | null>(null);
  useEffect(() => {
    setNote("");
    setCode("");
    setSecrets({});
    setSealing(null);
    setSealError(null);
  }, [ticket.id]);
  const link = phaseLink(ticket);
  const op = opAsk(ticket);
  const opOutcome = op ? opAskOutcome(op) : null;
  // A ticket with exactly one option is a "hands" ticket: something only the
  // person can do. The card says so, and the option reads as its button.
  const hands = ticket.options.length === 1;
  // A card that asks for a credential is never a pattern: "and similar"
  // would auto-resolve the next such ask with nothing sealed. The kernel
  // drops the flag too; hiding the box keeps the card honest.
  const hasAsk = ticket.options.some((o) => Boolean(o.credential_request));

  const sealAndDecide = async (o: TicketOption) => {
    const req = o.credential_request;
    if (!req) return;
    const value = (secrets[o.id] || "").trim();
    if (!value) {
      setSealError("Paste the credential first.");
      return;
    }
    setSealing(o.id);
    setSealError(null);
    // if_absent: the ask named a NEW key when it was raised; if a key of that
    // name has appeared since, the paste must not replace it (409).
    const r = await storeVaultSecret(req.name, req.cred_type, value, true);
    if (!r.ok) {
      setSealing(null);
      setSealError(sealErrorText(r.status, r.message, req.name));
      return;
    }
    setSecrets((s) => ({ ...s, [o.id]: "" }));
    setSealing(null);
    onDecide(o.id, sealedNote(req.name, note));
  };

  return (
    <>
      <MainHeader>
        <Pill tone="accent">{titleizeToken(ticket.source)}</Pill>
        <Pill>{ticket.circle_id != null ? `circle ${ticket.circle_id}` : "no circle"}</Pill>
        <Pill tone={priorityTone(ticket.priority)}>priority {ticket.priority}</Pill>
        <span className="ml-auto text-muted-foreground text-xs">
          {formatTime(ticket.opened_at)}
        </span>
        <button
          aria-label="Dismiss"
          className="text-muted-foreground hover:text-destructive"
          onClick={onDismiss}
          title="Dismiss without deciding"
          type="button"
        >
          <XIcon className="size-4" />
        </button>
      </MainHeader>
      <MainScroll className="p-6">
        <div className="mx-auto max-w-3xl">
          <div className="text-muted-foreground text-xs">
            From {requesterName(ticket)}
            {ticket.requester?.kind ? ` (${ticket.requester.kind})` : ""}
          </div>
          <p className="mt-2 whitespace-pre-wrap text-foreground text-sm leading-relaxed">
            {ticket.sitrep}
          </p>

          {ticket.context != null && (
            <details className="mt-4">
              <summary className="cursor-pointer text-muted-foreground text-xs">
                Context
              </summary>
              <pre className="mt-2 overflow-x-auto rounded-md border border-border bg-card p-3 text-xs">
                {JSON.stringify(ticket.context, null, 2)}
              </pre>
            </details>
          )}

          {op && (
            <div className="mt-4">
              {op.changes && (
                <details className="rounded-md border border-border" open={ticket.status === "open"}>
                  <summary className="cursor-pointer px-3 py-2 text-muted-foreground text-xs">
                    What it changes on {op.server}
                  </summary>
                  <pre className="max-h-72 overflow-auto whitespace-pre-wrap border-border border-t bg-card p-3 text-xs">
                    {op.changes}
                  </pre>
                </details>
              )}
              {opOutcome && <div className="mt-2 text-muted-foreground text-xs">{opOutcome}</div>}
              {op.runs.length > 0 && (
                <div className="mt-3">
                  <div className="font-medium text-muted-foreground text-xs uppercase tracking-wide">
                    Runs under this approval
                  </div>
                  <ul className="mt-1 space-y-1 text-xs">
                    {op.runs.map((r, i) => (
                      <li key={`${r.run ?? "r"}-${i}`}>
                        <span className={r.state === "applied" ? "text-foreground" : "text-destructive"}>
                          {r.state ?? "?"}
                        </span>
                        {r.at ? ` · ${formatTime(r.at)}` : ""}
                        {" · "}
                        <code>{JSON.stringify(r.params ?? {})}</code>
                        {r.verify_error ? ` — verify: ${r.verify_error}` : ""}
                        {r.error ? ` — ${r.error}` : ""}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}

          {(link.phaseId != null || link.gate) && (
            <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
              <Pill tone="accent">
                {link.gate ? "a build phase is waiting on you" : "linked to a build phase"}
              </Pill>
              {link.last && (
                <span className="text-muted-foreground">
                  The phase last reported <strong>{link.last.status}</strong>
                  {link.last.note ? `: ${link.last.note}` : ""}
                </span>
              )}
            </div>
          )}

          <div className="mt-6 mb-2 font-medium text-muted-foreground text-xs uppercase tracking-wide">
            {hands ? "When you have done it" : "Options"}
          </div>
          {!decidable && ticket.decision && (
            <div className="mb-2 text-muted-foreground text-xs">
              Decided: {ticket.decision.option_label || ticket.decision.option_id}
              {ticket.decision.note ? ` — ${ticket.decision.note}` : ""}
            </div>
          )}
          <div className="grid gap-2">
            {ticket.options.map((o) =>
              o.credential_request ? (
                // A credential ask: the person pastes it here, it is sealed
                // into Keys under the name the Host asked for, and the ticket
                // decides itself with that name. The Host never sees the value.
                <div
                  className={cn(
                    "rounded-lg border border-border px-4 py-3",
                    ticket.decision?.option_id === o.id && "border-primary bg-primary/10",
                  )}
                  key={o.id}
                >
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-foreground text-sm">
                      {o.label || o.id}
                    </span>
                    <Pill>asks for a credential</Pill>
                  </div>
                  {o.consequence && (
                    <div className="mt-0.5 text-muted-foreground text-xs">{o.consequence}</div>
                  )}
                  <div className="mt-2 text-muted-foreground text-xs">
                    Stored in Keys as <strong>{o.credential_request.name}</strong>
                    {o.credential_request.purpose ? ` — ${o.credential_request.purpose}` : ""}.
                    Your Host gets the name, never the value.
                  </div>
                  {decidable && (
                    <div className="mt-2 flex gap-2">
                      <input
                        aria-label={`Credential for ${o.credential_request.name}`}
                        autoComplete="off"
                        className="min-w-0 flex-1 rounded-md border border-border bg-background px-3 py-2 text-sm"
                        onChange={(e) =>
                          setSecrets((s) => ({ ...s, [o.id]: e.target.value }))
                        }
                        placeholder="Paste it here"
                        type="password"
                        value={secrets[o.id] || ""}
                      />
                      <Button
                        disabled={sealing === o.id}
                        onClick={() => sealAndDecide(o)}
                        size="sm"
                        type="button"
                      >
                        {sealing === o.id ? "Storing…" : "Store in Keys and mark done"}
                      </Button>
                    </div>
                  )}
                  {sealError && sealing === null && (
                    <div className="mt-1 text-destructive text-xs">{sealError}</div>
                  )}
                </div>
              ) : (
                <button
                  className={cn(
                    "rounded-lg border border-border px-4 py-3 text-left transition-colors",
                    decidable
                      ? "hover:border-primary hover:bg-primary/10"
                      : "cursor-default opacity-60",
                    ticket.decision?.option_id === o.id && "border-primary bg-primary/10",
                  )}
                  disabled={!decidable || (o.id === codeFor && !stepupCodeReady(code))}
                  key={o.id}
                  onClick={() =>
                    o.id === codeFor
                      ? onDecide(o.id, note.trim(), code.replace(/\s/g, ""))
                      : onDecide(o.id, note.trim())
                  }
                  type="button"
                >
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-foreground text-sm">
                      {o.label || o.id}
                    </span>
                    {o.recommend && <Pill tone="success">Recommended</Pill>}
                    {o.id === codeFor && <Pill>needs your code</Pill>}
                  </div>
                  {o.consequence && (
                    <div className="mt-0.5 text-muted-foreground text-xs">
                      {o.consequence}
                    </div>
                  )}
                </button>
              ),
            )}
          </div>
          {decidable && codeFor && (
            <label className="mt-3 block text-sm">
              <span className="text-foreground">Code from your authenticator</span>
              <span className="block text-muted-foreground text-xs">{stepup?.why}</span>
              <input
                aria-label="Authenticator code"
                autoComplete="one-time-code"
                className="mt-1 block w-40 rounded-md border border-border bg-background px-3 py-2 font-mono text-sm tracking-widest"
                inputMode="numeric"
                maxLength={7}
                onChange={(e) => setCode(e.target.value)}
                placeholder="123456"
                value={code}
              />
            </label>
          )}
          {decideError && (
            <div className="mt-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-destructive text-xs">
              {decideError}
            </div>
          )}

          {decidable && (
            <label className="mt-4 block text-sm">
              <span className="text-foreground">
                {hands ? "What you did, or why not" : "A note on your decision"}
              </span>
              <Textarea
                className="mt-1"
                onChange={(e) => setNote(e.target.value)}
                placeholder={
                  hands
                    ? "Optional — anything your Host should know before it continues."
                    : "Optional — recorded with the option you pick."
                }
                rows={3}
                value={note}
              />
            </label>
          )}

          {!hasAsk && (
            <label className="mt-4 flex cursor-pointer items-start gap-2 text-sm">
              <input
                checked={andSimilar}
                className="mt-0.5 size-4 accent-primary"
                disabled={!decidable}
                onChange={(e) => onToggleSimilar(e.target.checked)}
                type="checkbox"
              />
              <span className="min-w-0">
                <span className="text-foreground">And similar going forward</span>
                <span className="block text-muted-foreground text-xs">
                  Auto-resolve future tickets that match this shape without paging
                  you.
                </span>
              </span>
            </label>
          )}

          {ticket.status !== "open" && (
            <Button
              className="mt-4"
              onClick={onDismiss}
              size="sm"
              variant="outline"
            >
              Dismiss
            </Button>
          )}
        </div>
      </MainScroll>
    </>
  );
}

// ── Support ─────────────────────────────────────────────────────────────────

function CardSection({ title, body }: { title: string; body?: string }) {
  if (!body) return null;
  return (
    <section className="mt-4">
      <div className="mb-1 font-medium text-muted-foreground text-xs uppercase tracking-wide">
        {title}
      </div>
      <p className="whitespace-pre-wrap text-foreground text-sm leading-relaxed">{body}</p>
    </section>
  );
}

export function SupportDetail({
  ticket,
  onChanged,
  onClosed,
}: {
  ticket: SupportTicket;
  onChanged: () => void;
  onClosed: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [reply, setReply] = useState("");
  const [items, setItems] = useState<SharedItem[]>(ticket.shared_context ?? []);
  // Why Send reloaded the draft instead of sending it (the Host rewrote it
  // after it was opened here). Unlike `err`, it survives that reload.
  const [changedNote, setChangedNote] = useState<string | null>(null);

  useEffect(() => {
    setItems(ticket.shared_context ?? []);
    setErr(null);
  }, [ticket]);

  useEffect(() => {
    setChangedNote(null);
  }, [ticket.id]);

  const st = platformStatusLabel(ticket);
  const isDraft = ticket.status === "draft";
  const isSent = ticket.status === "sent";
  const includedCount = items.filter((i) => i.included).length;

  const run = async (fn: () => Promise<{ ok: boolean; message?: string }>) => {
    setBusy(true);
    setErr(null);
    const r = await fn();
    setBusy(false);
    if (!r.ok) setErr(r.message || "That didn't work.");
    return r.ok;
  };

  // The server's list is what Send reads, so the checkbox must show the
  // server's state: apply the response on success, revert on failure.
  const toggle = async (key: string, included: boolean) => {
    const before = items;
    setItems(items.map((i) => (i.key === key ? { ...i, included } : i)));
    setBusy(true);
    setErr(null);
    const r = await setSupportShared(ticket.id, { [key]: included });
    setBusy(false);
    if (r.ok) setItems(r.data.shared_context);
    else {
      setItems(before);
      setErr(r.message || "That didn't work — the item was not changed.");
    }
  };

  const send = async () => {
    if (
      !window.confirm(
        `Send this ticket to the platform with ${includedCount} of ${items.length} context item${items.length === 1 ? "" : "s"}? Only the ticked items leave this machine.`,
      )
    )
      return;
    setChangedNote(null);
    setBusy(true);
    setErr(null);
    const r = await sendSupport(ticket.id, sendRevision(ticket));
    setBusy(false);
    if (r.ok) {
      onChanged();
    } else if (r.status === 409) {
      // Not sent: the draft changed (or stopped being a draft) after it was
      // opened here. Show the new version and say why.
      setChangedNote(r.message || "This draft changed after you opened it. Nothing was sent.");
      onChanged();
    } else {
      setErr(r.message || "That didn't work.");
    }
  };

  const discard = async () => {
    if (!window.confirm("Discard this draft? Nothing was sent.")) return;
    if (await run(() => discardSupport(ticket.id))) onClosed();
  };

  const close = async () => {
    if (!window.confirm("Close this ticket on your side?")) return;
    if (await run(() => closeSupport(ticket.id))) onClosed();
  };

  const sendReply = async () => {
    const body = reply.trim();
    if (!body) return;
    if (await run(() => replySupport(ticket.id, body))) {
      setReply("");
      onChanged();
    }
  };

  return (
    <>
      <MainHeader>
        <Pill tone="accent">{titleizeToken(ticket.category)}</Pill>
        <Pill>{ticket.project_name ? ticket.project_name : "Host"}</Pill>
        {ticket.severity === "high" && <Pill tone="danger">blocking</Pill>}
        <Pill tone={st.tone}>{st.text}</Pill>
        <span className="ml-auto text-muted-foreground text-xs">
          {formatTime(ticket.opened_at)}
        </span>
        {(isDraft || isSent) && (
          <button
            aria-label={isDraft ? "Discard draft" : "Close ticket"}
            className="text-muted-foreground hover:text-destructive"
            onClick={isDraft ? discard : close}
            title={isDraft ? "Discard without sending" : "Close on your side"}
            type="button"
          >
            <XIcon className="size-4" />
          </button>
        )}
      </MainHeader>
      <MainScroll className="p-6">
        <div className="mx-auto max-w-3xl">
          <h2 className="font-medium text-foreground text-base">{ticket.title}</h2>
          {ticket.platform_ref && (
            <div className="mt-1 text-muted-foreground text-xs">
              Platform reference {ticket.platform_ref}
            </div>
          )}

          {err && (
            <div className="mt-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-destructive text-xs">
              {err}
            </div>
          )}
          {changedNote && (
            <div className="mt-3 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-foreground text-xs">
              {changedNote}
            </div>
          )}

          {(ticket.resolution || ticket.pr_url) && (
            <div className="mt-4 rounded-lg border border-border bg-card p-4">
              <div className="mb-1 font-medium text-muted-foreground text-xs uppercase tracking-wide">
                {ticket.platform_status === "rejected" ? "Why it was declined" : "Resolution"}
              </div>
              {ticket.resolution && (
                <p className="whitespace-pre-wrap text-foreground text-sm leading-relaxed">
                  {ticket.resolution}
                </p>
              )}
              {ticket.pr_url && (
                <a
                  className="mt-2 inline-block text-primary text-xs underline"
                  href={ticket.pr_url}
                  rel="noreferrer"
                  target="_blank"
                >
                  The change
                </a>
              )}
            </div>
          )}

          <CardSection body={ticket.problem} title="The problem" />
          <CardSection body={ticket.context} title="Context" />
          <CardSection body={ticket.recommendation} title="Your Host's recommendation" />
          <CardSection body={ticket.pushback} title="The pushback" />
          {(ticket.repro_steps || ticket.expected || ticket.actual) && (
            <details className="mt-4">
              <summary className="cursor-pointer text-muted-foreground text-xs">
                How to reproduce
              </summary>
              <CardSection body={ticket.repro_steps} title="Steps" />
              <CardSection body={ticket.expected} title="Expected" />
              <CardSection body={ticket.actual} title="Actual" />
            </details>
          )}

          {/* What will be / was shared */}
          <section className="mt-6">
            <div className="mb-1 font-medium text-muted-foreground text-xs uppercase tracking-wide">
              {isDraft ? "What will be shared" : "What was shared"}
            </div>
            <p className="mb-2 text-muted-foreground text-xs">
              {isDraft
                ? "Your Host gathered these. Untick anything you don't want the platform to see. Secrets were already cut."
                : "The items the platform received. Nothing else left this machine."}
            </p>
            {items.length === 0 ? (
              <div className="text-muted-foreground text-xs">Nothing beyond the card itself.</div>
            ) : (
              <div className="grid gap-2">
                {items.map((it) => (
                  <details
                    className={cn(
                      "rounded-lg border border-border px-3 py-2",
                      !it.included && "opacity-60",
                    )}
                    key={it.key}
                  >
                    <summary className="flex cursor-pointer items-center gap-2 text-sm">
                      <input
                        checked={it.included}
                        className="size-4 accent-primary"
                        disabled={!isDraft || busy}
                        onChange={(e) => toggle(it.key, e.target.checked)}
                        onClick={(e) => e.stopPropagation()}
                        type="checkbox"
                      />
                      <span className="min-w-0 flex-1 truncate">{it.label}</span>
                      <span className="text-muted-foreground text-xs">
                        {it.content.length.toLocaleString()} chars
                      </span>
                    </summary>
                    <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-card p-2 text-xs">
                      {it.content || "(empty)"}
                    </pre>
                  </details>
                ))}
              </div>
            )}
          </section>

          {isDraft && (
            <div className="mt-4 flex items-center gap-2">
              <Button disabled={busy} onClick={send} size="sm">
                <SendIcon className="mr-1 size-4" />
                Send to the platform
              </Button>
              <Button disabled={busy} onClick={discard} size="sm" variant="outline">
                Discard
              </Button>
              <span className="text-muted-foreground text-xs">
                {includedCount} of {items.length} item{items.length === 1 ? "" : "s"} will be shared.
              </span>
            </div>
          )}

          {/* Conversation */}
          {(isSent || ticket.status === "closed") && (
            <section className="mt-6">
              <div className="mb-1 font-medium text-muted-foreground text-xs uppercase tracking-wide">
                Conversation
              </div>
              {(ticket.thread ?? []).length === 0 ? (
                <div className="text-muted-foreground text-xs">Nothing yet.</div>
              ) : (
                <div className="grid gap-2">
                  {(ticket.thread ?? []).map((m, i) => (
                    <div
                      className={cn(
                        "rounded-lg border border-border px-3 py-2",
                        m.author === "platform" ? "bg-card" : "bg-primary/5",
                      )}
                      key={`${m.at}-${i}`}
                    >
                      <div className="text-muted-foreground text-xs">
                        {m.author === "platform"
                          ? "Platform"
                          : m.author === "host"
                            ? "Your Host"
                            : "You"}{" "}
                        · {formatTime(m.at)}
                      </div>
                      <p className="mt-1 whitespace-pre-wrap text-foreground text-sm">{m.body}</p>
                    </div>
                  ))}
                </div>
              )}
              {isSent && (
                <div className="mt-3">
                  <Textarea
                    disabled={busy}
                    onChange={(e) => setReply(e.target.value)}
                    placeholder={
                      ticket.platform_status === "needs_info"
                        ? "Answer the platform's question…"
                        : "Add to the ticket…"
                    }
                    rows={3}
                    value={reply}
                  />
                  <div className="mt-2 flex items-center gap-2">
                    <Button disabled={busy || !reply.trim()} onClick={sendReply} size="sm">
                      Reply
                    </Button>
                    {/* "Close ticket", not "Close": in a sheet, the sheet's own
                        Close sits above it and only shuts the view. */}
                    <Button data-support-close disabled={busy} onClick={close} size="sm" variant="outline">
                      Close ticket
                    </Button>
                  </div>
                </div>
              )}
            </section>
          )}
        </div>
      </MainScroll>
    </>
  );
}

// The user filing directly. The Host usually does this with more context; this
// is the plain form for "just let me tell them".
export function SupportCompose({
  onCancel,
  onCreated,
}: {
  onCancel: () => void;
  onCreated: (id: number) => void;
}) {
  const [category, setCategory] = useState<SupportCategory>("question");
  const [title, setTitle] = useState("");
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async () => {
    if (!title.trim() || !problem.trim()) return;
    setBusy(true);
    setErr(null);
    const r = await openSupport({ category, title: title.trim(), problem: problem.trim() });
    setBusy(false);
    if (r.ok) onCreated(r.data.ticket_id);
    else setErr(r.message || "Couldn't draft the ticket.");
  };

  return (
    <>
      <MainHeader>
        <Pill tone="accent">New support ticket</Pill>
        <button
          aria-label="Cancel"
          className="ml-auto text-muted-foreground hover:text-foreground"
          onClick={onCancel}
          type="button"
        >
          <XIcon className="size-4" />
        </button>
      </MainHeader>
      <MainScroll className="p-6">
        <div className="mx-auto grid max-w-3xl gap-3">
          <p className="text-muted-foreground text-xs">
            This drafts a ticket. You'll see exactly what would be shared before anything is
            sent. Your Host can write a fuller one for you — just ask it.
          </p>
          <FilterSelect
            ariaLabel="Category"
            onChange={(v) => setCategory(v as SupportCategory)}
            options={CATEGORIES}
            value={category}
          />
          <input
            className="rounded-md border border-border bg-background px-3 py-2 text-sm"
            onChange={(e) => setTitle(e.target.value)}
            placeholder="One line — the way you'd say it"
            value={title}
          />
          <Textarea
            onChange={(e) => setProblem(e.target.value)}
            placeholder="What's wrong, or what you need"
            rows={6}
            value={problem}
          />
          {err && <div className="text-destructive text-xs">{err}</div>}
          <div className="flex items-center gap-2">
            <Button disabled={busy || !title.trim() || !problem.trim()} onClick={submit} size="sm">
              Draft it
            </Button>
            <Button disabled={busy} onClick={onCancel} size="sm" variant="outline">
              Cancel
            </Button>
          </div>
        </div>
      </MainScroll>
    </>
  );
}



// ── The Decisions sheet ─────────────────────────────────────────────────────

/**
 * Every decision your Host asked of you, the Tickets pane's queue (plan,
 * "Tickets → Waiting on you": open decisions are Waiting on you, each on the
 * page that asked; this sheet is the rest of the pane): the decided,
 * auto-resolved and dismissed ones, any one in full with its context, and
 * the decide and Dismiss for one asked nowhere a page can show (an app's
 * ticket, an import, a support card on the operator Host, a page since
 * deleted). Opened from Waiting on you, from an ask card's Details, and for
 * a decision whose page cannot show it.
 */
export function DecisionsSheet({
  focusId,
  onClose,
  onChanged,
}: {
  /** A decision to open in full. */
  focusId: number | null;
  onClose: () => void;
  /** Decision `id` was decided or dismissed: its ask card goes, and what
   * waits is read again. */
  onChanged: (id: number) => void;
}) {
  const [filter, setFilter] = useState(focusId != null ? "" : "open");
  const [tickets, setTickets] = useState<Ticket[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState<Ticket | null>(null);
  const [andSimilar, setAndSimilar] = useState(false);
  const [decideError, setDecideError] = useState<string | null>(null);
  const [dismissing, setDismissing] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    const r = await listTickets(filter || undefined);
    if (r.ok) setTickets(r.data.tickets ?? []);
    else {
      setError(`${r.status || ""} ${r.message}`.trim());
      setTickets([]);
    }
  }, [filter]);
  useEffect(() => {
    void load();
  }, [load]);

  const open = useCallback(async (id: number, fallback?: Ticket) => {
    setAndSimilar(false);
    setDecideError(null);
    setDismissing(false);
    const r = await getTicket(id);
    if (r.ok) setActive(r.data);
    else if (fallback) setActive(fallback);
    else setDecideError(`Couldn't read decision #${id}: ${r.message}`);
  }, []);
  useEffect(() => {
    if (focusId != null) void open(focusId);
  }, [focusId, open]);

  // The pane's own decide (tickets-pane.tsx decide): the support card's mark
  // goes back, and a decide the platform recorded but did not apply says so.
  const decide = async (optionId: string, note = "", stepupCode: string | null = null) => {
    if (!active) return;
    setDecideError(null);
    const r = await decideTicket(active.id, optionId, andSimilar, note, supportMark(active), stepupCode);
    if (r.ok) {
      const support = (r.data as { support?: { applied?: boolean; error?: string; final?: boolean } }).support;
      if (support && support.applied === false && support.error) {
        setDecideError(
          support.final
            ? `Recorded, but the platform refused it for good: ${support.error}`
            : `Recorded, but not applied yet: ${support.error}`,
        );
      }
      await load();
      const fresh = await getTicket(active.id);
      if (fresh.ok) setActive(fresh.data);
      onChanged(active.id);
    } else {
      setDecideError(`${r.status || ""} ${r.message}`.trim());
      const fresh = await getTicket(active.id);
      if (fresh.ok) setActive(fresh.data);
    }
  };

  const dismiss = async () => {
    if (!active) return;
    if (!dismissing) {
      setDismissing(true);
      return;
    }
    const r = await dismissTicket(active.id);
    setDismissing(false);
    if (!r.ok) {
      setDecideError(`Couldn't dismiss it: ${r.message}`);
      return;
    }
    const id = active.id;
    setActive(null);
    await load();
    onChanged(id);
  };

  return (
    <Sheet data="decisions" onClose={onClose} title={active ? `Decision #${active.id}` : "Decisions"}>
      {active ? (
        <div className="flex min-h-0 flex-1 flex-col" data-decision={active.id} data-decision-status={active.status}>
          <div className="sheetbar">
            <button className="link" data-decisions-back onClick={() => setActive(null)} type="button">
              ‹ All decisions
            </button>
            {dismissing && (
              <span className="small" data-dismiss-ask>
                Dismiss it without deciding? Press Dismiss again.{" "}
                <button className="link" onClick={() => setDismissing(false)} type="button">
                  Not now
                </button>
              </span>
            )}
          </div>
          <TicketDetail
            andSimilar={andSimilar}
            decideError={decideError}
            onDecide={decide}
            onDismiss={() => void dismiss()}
            onToggleSimilar={setAndSimilar}
            ticket={active}
          />
        </div>
      ) : (
        <>
          <div className="sheetbar">
            <FilterSelect ariaLabel="Which decisions" onChange={setFilter} options={DECISION_FILTERS} value={filter} />
          </div>
          {error ? (
            <ErrorState message={error} onRetry={() => void load()} />
          ) : tickets == null ? (
            <LoadingLine />
          ) : tickets.length === 0 ? (
            <p className="small" style={{ padding: 16 }}>
              {filter === "open" ? "Nothing to decide." : "No decisions in this filter."}
            </p>
          ) : (
            <ul className="sheetlist" data-decisions-list>
              {tickets.map((t) => (
                <li key={t.id}>
                  <button data-decision-row={t.id} onClick={() => void open(t.id, t)} type="button">
                    <span className="flex items-center justify-between gap-2">
                      <span className="truncate text-muted-foreground text-xs">
                        {titleizeToken(t.source)} · {titleizeToken(t.status)}
                      </span>
                      <Pill tone={priorityTone(t.priority)}>P{t.priority}</Pill>
                    </span>
                    <span className="block truncate font-medium text-sm">{t.sitrep}</span>
                    <span className="block truncate text-muted-foreground text-xs">
                      {requesterName(t)} · {formatTime(t.opened_at)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {decideError && (
            <p className="err" role="alert" style={{ padding: "0 16px" }}>
              {decideError}
            </p>
          )}
        </>
      )}
    </Sheet>
  );
}

// ── A support ticket in full, and a new one ─────────────────────────────────

/**
 * One support ticket as the Tickets pane showed it (SupportDetail): the
 * platform's status in words, the conversation with the platform's team and
 * your reply to it, what was shared (each item's content before you send),
 * the change link, Discard for a draft and Close for a sent one. Or, with no
 * ticket, a new one (SupportCompose): what is wrong, which your Host drafts
 * for you to approve. Opened from the Support card.
 */
export function SupportSheet({
  ticketId,
  onClose,
  onChanged,
  onCreated,
}: {
  /** The ticket to show, or null to write a new one. */
  ticketId: number | null;
  onClose: () => void;
  /** It changed (sent, replied, discarded, closed): read the card again. */
  onChanged: () => void;
  /** A new ticket was drafted: show it. */
  onCreated: (id: number) => void;
}) {
  const [t, setT] = useState<SupportTicket | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const read = useCallback(async () => {
    if (ticketId == null) return;
    const r = await getSupport(ticketId);
    if (r.ok) {
      setT(r.data);
      setErr(null);
    } else setErr(r.message || "Couldn't read the ticket.");
  }, [ticketId]);
  useEffect(() => {
    setT(null);
    void read();
  }, [read]);
  return (
    <Sheet data="support" onClose={onClose} title={ticketId == null ? "A new support ticket" : t?.title || "Support"}>
      <div className="flex min-h-0 flex-1 flex-col" data-support-sheet={ticketId ?? "new"}>
        {ticketId == null ? (
          <SupportCompose onCancel={onClose} onCreated={(id) => onCreated(id)} />
        ) : err ? (
          <ErrorState message={err} onRetry={() => void read()} />
        ) : !t ? (
          <LoadingLine />
        ) : (
          <SupportDetail
            onChanged={() => {
              void read();
              onChanged();
            }}
            onClosed={() => {
              onChanged();
              onClose();
            }}
            ticket={t}
          />
        )}
      </div>
    </Sheet>
  );
}
