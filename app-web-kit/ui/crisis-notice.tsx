"use client"

// The platform's crisis notice. When the platform's crisis
// check flags the person's words, the turn runs as any other: the Host answers
// in its own words, told the check fired. The person gets THIS — the
// platform's own note, never a message in the thread and never in the Host's
// voice — with the 988 resources, as a pop-up warning the person can dismiss.
//
// Not modal: no backdrop, focus is not trapped, the thread scrolls and the
// reply streams under it. Pinned to the top (phone: the full width, at most
// 45% of the screen; desktop: a card at the top right), away from the reply
// and the box at the bottom. Escape or Dismiss closes it. It stays up across a
// reload until dismissed (../crisis-notice.ts).

import * as React from "react"

import { cn } from "@/lib/utils"
import {
  CRISIS_NOTICE_KEY,
  type CrisisNoticeState,
  dismissCrisisNotice,
  parseCrisisNotice,
  raiseCrisisNotice,
} from "../crisis-notice"
import { Button } from "./button"

function readState(): CrisisNoticeState {
  try {
    return parseCrisisNotice(window.localStorage.getItem(CRISIS_NOTICE_KEY))
  } catch {
    return { pending: null, dismissed: [] }
  }
}

function writeState(s: CrisisNoticeState): void {
  try {
    window.localStorage.setItem(CRISIS_NOTICE_KEY, JSON.stringify(s))
  } catch {
    /* private mode: the notice lasts for this page only */
  }
}

/** The notice's state for a chat hook: `raise(turnKey)` on the governance
 * frame, `dismiss()` from the notice. Read from this device on mount, so a
 * notice not yet dismissed comes back after a reload. */
export function useCrisisNotice() {
  const [state, setState] = React.useState<CrisisNoticeState>(() =>
    typeof window === "undefined" ? { pending: null, dismissed: [] } : readState(),
  )
  const raise = React.useCallback((turnKey: string) => {
    // Read back first: another tab may have dismissed it meanwhile.
    const next = raiseCrisisNotice(readState(), turnKey)
    writeState(next)
    setState(next)
  }, [])
  const dismiss = React.useCallback(() => {
    const next = dismissCrisisNotice(readState())
    writeState(next)
    setState(next)
  }, [])
  return { open: state.pending !== null, raise, dismiss }
}

export interface CrisisNoticeProps {
  open: boolean
  onDismiss: () => void
  className?: string
}

export function CrisisNotice({ open, onDismiss, className }: CrisisNoticeProps) {
  const onDismissRef = React.useRef(onDismiss)
  onDismissRef.current = onDismiss

  React.useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onDismissRef.current()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open])

  if (!open) return null
  return (
    <section
      aria-describedby="crisis-notice-body"
      aria-labelledby="crisis-notice-title"
      aria-modal="false"
      className={cn(
        "fixed inset-x-2 top-2 z-50 max-h-[45vh] overflow-auto rounded-lg border-2 border-sky-600/70 bg-background p-4 text-foreground text-sm shadow-lg",
        "sm:inset-x-auto sm:top-4 sm:right-4 sm:w-[22rem] sm:max-h-[70vh]",
        className,
      )}
      data-crisis-notice
      role="dialog"
    >
      <p className="font-semibold text-[11px] text-muted-foreground uppercase tracking-wide">
        From the platform · not your Host
      </p>
      <h2 className="mt-1 font-semibold text-base" id="crisis-notice-title">
        Support is available
      </h2>
      <div className="mt-2 space-y-2" id="crisis-notice-body">
        <p>
          The platform’s safety check flagged a message in this conversation. If you’re thinking
          about suicide or hurting yourself, call or text 988 (in the US), or chat at{" "}
          <a
            className="underline underline-offset-2"
            href="https://988lifeline.org/chat/"
            rel="noopener noreferrer"
            target="_blank"
          >
            988lifeline.org
          </a>{" "}
          — the 988 Suicide &amp; Crisis Lifeline. It’s free, confidential, and open 24/7. If you
          can, tell someone you trust too.
        </p>
        <p className="text-muted-foreground text-xs">
          If this doesn’t apply, dismiss it. Your Host was told the check fired; its reply is its own.
        </p>
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button asChild size="sm">
          <a data-crisis-call href="tel:988">
            Call 988
          </a>
        </Button>
        <Button asChild size="sm" variant="outline">
          <a data-crisis-text href="sms:988">
            Text 988
          </a>
        </Button>
        <Button data-crisis-dismiss onClick={onDismiss} size="sm" type="button" variant="ghost">
          Dismiss
        </Button>
      </div>
    </section>
  )
}
