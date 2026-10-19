"use client"

// The ask card as the person sees it: an overlay over the chat, not a message
// in it: the approval card is an overlay over the chat, never a thread
// message. One sentence from the
// Host, the choices, the recommended one marked, a line for their own words,
// and "Not now". The card is the decision ticket the Host filed; answering
// here decides it exactly as the Tickets pane would.
//
// So the person never chases apps in the middle of a conversation, a
// credential ask is answered HERE when the app
// gives onSealCredential — the sealed field and "Store in Keys", the value
// going browser → vault and the Host getting only the key's name — instead of
// sending the person to Circles › Tickets.

import * as React from "react"

import { cn } from "@/lib/utils"
import {
  type Ask,
  type AskCredentialRequest,
  type AskOption,
  sealedNote,
  stepupCodeReady,
} from "../ask"
import { Badge } from "./badge"
import { Button } from "./button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./dialog"
import { Textarea } from "./textarea"

export interface AskOverlayProps {
  /** The card to show; null renders nothing. */
  ask: Ask | null
  /** How many more asks wait behind this one. */
  waiting?: number
  busy?: boolean
  error?: string | null
  /** `stepupCode` rides only the option the card takes the authenticator
   * code for (ask.stepupOption); the kernel checks and burns it. */
  onAnswer: (option: AskOption, note: string, stepupCode?: string) => void
  /** Dismiss: the ticket stays open, in Waiting on you; the Host is told when a turn
   * is running, with the person's note. Also fired when the dialog is closed
   * any other way (Escape, the backdrop). */
  onNotNow: (note: string) => void
  /** Seal a credential ask's value into Keys (the app's own POST
   * /api/vault/secrets with if_absent, so an existing key is never
   * replaced). Resolves to null when stored, else the line that says why
   * not. Given → the card hosts the sealed field and "Store in Keys", and on
   * success decides the ticket with the key's NAME only (sealedNote): the
   * value goes browser → vault and nowhere else. */
  onSealCredential?: (req: AskCredentialRequest, value: string) => Promise<string | null>
  /** Given → the footer offers the decision in full (its context, Dismiss,
   * "and similar going forward"): Circles' Decisions sheet. */
  onDetails?: (ticketId: number) => void
}

export function AskOverlay({
  ask,
  waiting = 0,
  busy = false,
  error = null,
  onAnswer,
  onNotNow,
  onSealCredential,
  onDetails,
}: AskOverlayProps) {
  const [note, setNote] = React.useState("")
  const [code, setCode] = React.useState("")
  // One sealed field per credential option; the value never leaves this
  // component except into the vault call.
  const [secrets, setSecrets] = React.useState<Record<string, string>>({})
  const [sealing, setSealing] = React.useState<string | null>(null)
  const [sealError, setSealError] = React.useState<string | null>(null)
  // A fresh card starts with an empty note, code and field; the previous
  // card's words never ride onto the next ticket.
  React.useEffect(() => {
    setNote("")
    setCode("")
    setSecrets({})
    setSealing(null)
    setSealError(null)
  }, [ask?.ticketId])
  if (!ask) return null
  const locked = busy || sealing !== null
  const sealAndAnswer = async (o: AskOption) => {
    const req = o.credential_request
    if (!req || !onSealCredential) return
    const value = (secrets[o.id] || "").trim()
    if (!value) {
      setSealError("Paste it first.")
      return
    }
    setSealing(o.id)
    setSealError(null)
    const why = await onSealCredential(req, value)
    setSealing(null)
    if (why) {
      setSealError(why)
      return
    }
    setSecrets((s) => ({ ...s, [o.id]: "" }))
    onAnswer(o, sealedNote(req.name, note))
  }
  return (
    <Dialog onOpenChange={(open) => { if (!open && !locked) onNotNow(note) }} open>
      <DialogContent
        aria-describedby="ask-overlay-sitrep"
        className="sm:max-w-lg"
        data-ask-ticket={ask.ticketId}
        showCloseButton={!locked}
      >
        <DialogHeader>
          <DialogTitle>Your Host is asking</DialogTitle>
          <DialogDescription className="whitespace-pre-wrap text-foreground" id="ask-overlay-sitrep">
            {ask.sitrep || "Your Host needs you to choose."}
          </DialogDescription>
        </DialogHeader>
        {ask.attachment && (
          // The dry run's diff, folded: the
          // person can read exactly what lands before they click; the words
          // above are enough to answer without opening it.
          <details className="rounded-md border border-border" data-ask-attachment="">
            <summary className="cursor-pointer px-3 py-2 text-muted-foreground text-xs">
              {ask.attachment.label || "Details"}
            </summary>
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap border-border border-t bg-card p-3 text-xs">
              {ask.attachment.text}
            </pre>
          </details>
        )}
        <div className="grid grid-cols-[minmax(0,1fr)] gap-2 wrap-anywhere" role="group" aria-label="Choices">
          {ask.options.map((o) =>
            o.credential_request ? (
              <div className="rounded-lg border border-border px-4 py-3" key={o.id}>
                <div className="flex items-center gap-2">
                  <span className="font-medium text-sm">{o.label}</span>
                  <Badge variant="secondary">asks for a credential</Badge>
                </div>
                {o.consequence && (
                  <div className="mt-0.5 text-muted-foreground text-xs">{o.consequence}</div>
                )}
                <div className="mt-2 text-muted-foreground text-xs">
                  Stored in Keys as <strong>{o.credential_request.name}</strong>
                  {o.credential_request.purpose ? ` — ${o.credential_request.purpose}` : ""}.
                  {" "}
                  {onSealCredential
                    ? "It is sealed straight into Keys; your Host gets the name, never the value."
                    : "Answer this one in Circles, from Waiting on you, where it is sealed; your Host gets the name, never the value."}
                </div>
                {onSealCredential ? (
                  <div className="mt-2 flex gap-2">
                    <input
                      aria-label={`Credential for ${o.credential_request.name}`}
                      autoComplete="off"
                      className="min-w-0 flex-1 rounded-md border border-border bg-background px-3 py-2 text-sm"
                      data-ask-secret=""
                      disabled={locked}
                      onChange={(e) => setSecrets((s) => ({ ...s, [o.id]: e.target.value }))}
                      placeholder="Paste it here"
                      type="password"
                      value={secrets[o.id] || ""}
                    />
                    <Button
                      disabled={locked}
                      onClick={() => void sealAndAnswer(o)}
                      size="sm"
                      type="button"
                    >
                      {sealing === o.id ? "Storing…" : "Store in Keys"}
                    </Button>
                  </div>
                ) : null}
              </div>
            ) : (
              <button
                className={cn(
                  "rounded-lg border border-border px-4 py-3 text-left transition-colors",
                  "hover:border-primary hover:bg-primary/10 disabled:cursor-default disabled:opacity-60",
                )}
                disabled={locked || (o.id === ask.stepupOption && !stepupCodeReady(code))}
                key={o.id}
                onClick={() =>
                  o.id === ask.stepupOption
                    ? onAnswer(o, note, code.replace(/\s/g, ""))
                    : onAnswer(o, note)
                }
                type="button"
              >
                <div className="flex items-center gap-2">
                  <span className="font-medium text-sm">{o.label}</span>
                  {o.recommend && <Badge>Recommended</Badge>}
                  {o.id === ask.stepupOption && <Badge variant="secondary">needs your code</Badge>}
                </div>
                {o.consequence && (
                  <div className="mt-0.5 text-muted-foreground text-xs">{o.consequence}</div>
                )}
              </button>
            ),
          )}
        </div>
        {sealError && (
          <div className="text-destructive text-xs" role="alert">
            {sealError}
          </div>
        )}
        {ask.stepupOption && (
          <label className="block text-sm">
            <span className="text-muted-foreground text-xs">
              Code from your authenticator —{" "}
              {(ask.stepupWhy || "This adds a check that runs outside the platform.")
                .replace(/^./, (c) => c.toLowerCase())}
            </span>
            <input
              aria-label="Authenticator code"
              autoComplete="one-time-code"
              className="mt-1 block w-40 rounded-md border border-border bg-background px-3 py-2 font-mono text-sm tracking-widest"
              disabled={locked}
              inputMode="numeric"
              maxLength={7}
              onChange={(e) => setCode(e.target.value)}
              placeholder="123456"
              value={code}
            />
          </label>
        )}
        <label className="block text-sm">
          <span className="text-muted-foreground text-xs">A note for your Host (optional)</span>
          <Textarea
            className="mt-1 min-h-16"
            disabled={locked}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Anything it should know before it acts"
            value={note}
          />
        </label>
        {error && (
          <div
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-destructive text-xs"
            role="alert"
          >
            {error}
          </div>
        )}
        <DialogFooter className="items-center sm:justify-between">
          <span className="text-muted-foreground text-xs">
            {waiting > 0
              ? `${waiting} more ${waiting === 1 ? "question" : "questions"} waiting`
              : `Ticket #${ask.ticketId} — also in Circles, under Waiting on you`}
          </span>
          <span className="flex items-center gap-1">
            {onDetails && (
              <Button
                data-ask-details={ask.ticketId}
                disabled={locked}
                onClick={() => onDetails(ask.ticketId)}
                type="button"
                variant="ghost"
              >
                In full
              </Button>
            )}
            <Button disabled={locked} onClick={() => onNotNow(note)} type="button" variant="ghost">
              Not now
            </Button>
          </span>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
