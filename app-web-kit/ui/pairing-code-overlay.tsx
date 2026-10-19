"use client"

// The pairing code card (2026-10-05). When the Host starts a messaging
// connector's pairing, the chat shows the one-time code here instead of
// sending the person to the Network app, so they never chase apps in the
// middle of a conversation.
//
// The code never passes through the Host: the tool result carries only the
// grant id, and this card reads the code from GET
// /api/network/nac/connector/{id}/code, which answers the person's own windows
// only (server/routes/network.py _person_window). The code lives in the
// kernel's memory for ten minutes; the card counts down from `seconds_left`.
//
// It re-reads the route while it is up. A 404 then means the pairing is no
// longer open — most often because the bot reported the code, and the approval
// card is about to come up in this chat — so the card closes itself ("done").
// A 410 means the kernel restarted and the code is gone; the card says so.

import * as React from "react"

import type { ApiResult } from "../api"
import { countdownText, type PairingAsk, pairingInstruction } from "../ask"
import { Button } from "./button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./dialog"

export interface PairingCode {
  code: string
  seconds_left: number
  platform?: string
}

export interface PairingCodeOverlayProps {
  pairing: PairingAsk | null
  fetchCode: (grantId: number) => Promise<ApiResult<PairingCode>>
  /** "dismissed": the person closed it (Close, Escape, the backdrop).
   * "done": the pairing is no longer open (the bot reported the code, or it
   * was declined or replaced). */
  onClose: (reason: "dismissed" | "done") => void
  /** How often the open pairing is re-read, ms. */
  pollMs?: number
}

type View =
  | { kind: "loading" }
  | { kind: "code"; code: string; endsAt: number; platform: string }
  | { kind: "expired" }
  | { kind: "lost"; message: string }
  | { kind: "error"; message: string }

export function PairingCodeOverlay({
  pairing,
  fetchCode,
  onClose,
  pollMs = 4000,
}: PairingCodeOverlayProps) {
  const [view, setView] = React.useState<View>({ kind: "loading" })
  const [now, setNow] = React.useState(() => Date.now())
  const grantId = pairing?.grantId ?? null
  const onCloseRef = React.useRef(onClose)
  onCloseRef.current = onClose

  React.useEffect(() => {
    if (grantId == null) return
    let alive = true
    let shown = false
    let endsAt = 0
    setView({ kind: "loading" })
    const read = async () => {
      if (!alive) return
      const r = await fetchCode(grantId)
      if (!alive) return
      if (r.ok) {
        shown = true
        endsAt = Date.now() + Math.max(0, r.data.seconds_left) * 1000
        setView({
          kind: "code",
          code: r.data.code,
          endsAt,
          platform: r.data.platform || pairing?.platform || "",
        })
        return
      }
      if (r.status === 404) {
        alive = false
        // Not open any more. Past its ten minutes, that is the code running
        // out: say so. Otherwise, after the code was on screen, it is the
        // pairing moving on (the approval card is next); before, there is
        // nothing to show.
        if (shown && Date.now() >= endsAt) setView({ kind: "expired" })
        else onCloseRef.current(shown ? "done" : "dismissed")
        return
      }
      if (r.status === 410) {
        alive = false
        setView({ kind: "lost", message: r.message })
        return
      }
      if (!shown) setView({ kind: "error", message: `${r.status || ""} ${r.message}`.trim() })
    }
    void read()
    const poll = window.setInterval(() => void read(), pollMs)
    const tick = window.setInterval(() => setNow(Date.now()), 1000)
    return () => {
      alive = false
      window.clearInterval(poll)
      window.clearInterval(tick)
    }
    // pairing.platform is only a fallback label; the grant id is the identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [grantId, fetchCode, pollMs])

  if (!pairing) return null
  const left = view.kind === "code" ? Math.round((view.endsAt - now) / 1000) : 0
  const expired = view.kind === "expired" || (view.kind === "code" && left <= 0)
  const platform = view.kind === "code" ? view.platform : pairing.platform
  return (
    <Dialog onOpenChange={(open) => { if (!open) onClose("dismissed") }} open>
      <DialogContent
        aria-describedby="pairing-code-what"
        className="sm:max-w-md"
        data-pairing-grant={pairing.grantId}
      >
        <DialogHeader>
          <DialogTitle>Pair your bot</DialogTitle>
          <DialogDescription className="text-foreground" id="pairing-code-what">
            {expired
              ? "This code ran out. Ask your Host to start the pairing again."
              : view.kind === "lost"
                ? view.message ||
                  "This code was lost when your Host restarted. Ask your Host to start the pairing again."
                : pairingInstruction(platform)}
          </DialogDescription>
        </DialogHeader>
        {view.kind === "loading" && (
          <div className="py-4 text-center text-muted-foreground text-sm">Getting the code…</div>
        )}
        {view.kind === "error" && (
          <div
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-destructive text-xs"
            role="alert"
          >
            Couldn’t get the code — {view.message}. It is also in Network › Ingress.
          </div>
        )}
        {view.kind === "code" && !expired && (
          <div className="flex flex-col items-center gap-2 py-2">
            <div
              aria-label="Pairing code"
              className="select-all rounded-lg border border-border bg-card px-6 py-3 font-mono text-3xl tracking-[0.3em]"
              data-pairing-code=""
            >
              {view.code}
            </div>
            <div className="text-muted-foreground text-xs" aria-live="polite">
              Expires in {countdownText(left)}. Then an approval card comes up here; you let the
              account in with your authenticator code.
            </div>
          </div>
        )}
        <DialogFooter>
          <Button onClick={() => onClose("dismissed")} type="button" variant="ghost">
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
