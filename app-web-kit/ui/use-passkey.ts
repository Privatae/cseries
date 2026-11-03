"use client"

// The passkey a card offers (../passkey.ts), read from the Host when the chat
// opens and again whenever its window comes back into focus, so a passkey
// set up in another tab is offered on the next card without a reload.

import * as React from "react"

import {
  type PasskeyApprover,
  type PasskeyStatus,
  passkeyMode,
  passkeyStatus,
  ticketProof,
  webAuthnAvailable,
} from "../passkey.ts"

export function usePasskeyApprover(): PasskeyApprover {
  const [status, setStatus] = React.useState<PasskeyStatus | null>(null)
  React.useEffect(() => {
    let live = true
    const read = () => {
      void passkeyStatus().then((r) => {
        if (live && r.ok) setStatus(r.data)
      })
    }
    read()
    window.addEventListener("focus", read)
    return () => {
      live = false
      window.removeEventListener("focus", read)
    }
  }, [])
  return React.useMemo(
    () => ({
      mode: passkeyMode(status, webAuthnAvailable()),
      setupUrl: status?.setup_url || "/passkey",
      prove: ticketProof,
    }),
    [status],
  )
}
