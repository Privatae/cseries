// Approving a card with a passkey tap (server/approval_passkeys.py).
//
// The card asks the Host for a challenge made for exactly this answer (the
// ticket, the option, the boxes ticked: the server computes what that means
// from the ticket it holds), the device asks the person (face, fingerprint,
// PIN), and the signed answer rides the decide in the code's slot as
// "passkey:<json>". The server checks it before anything happens, once. The
// authenticator code stays as the fallback, behind "Use a code instead".
//
// The passkey is the owner's own, the one they sign in with, set up on the
// desktop's /passkey page; a person with none sees the code box and a line
// offering to set one up.

import { type ApiResult, getJSON, request } from "./api.ts";

export const PROOF_PREFIX = "passkey:";

/** What the card offers for an act that needs the person:
 *  approve      a passkey on this address and a browser that can use it
 *  setup        no passkey here yet: the code, and the offer to set one up
 *  unsupported  this browser cannot use a passkey: the code alone
 *  unknown      not read yet (or the read failed): the code, as before */
export type PasskeyMode = "approve" | "setup" | "unsupported" | "unknown";

export interface PasskeyStatus {
  passkey: boolean;
  code: boolean;
  setup_url: string;
}

export function passkeyMode(status: PasskeyStatus | null, canUse: boolean): PasskeyMode {
  if (!status) return "unknown";
  if (!canUse) return "unsupported";
  return status.passkey ? "approve" : "setup";
}

export function webAuthnAvailable(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof (window as unknown as { PublicKeyCredential?: unknown }).PublicKeyCredential !== "undefined" &&
    typeof navigator !== "undefined" &&
    !!navigator.credentials
  );
}

// ── base64url, and the JSON the server's webauthn library reads ───────────

export function b64uToBytes(s: string): Uint8Array {
  const b = s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4);
  const bin = atob(b);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export function bytesToB64u(buf: ArrayBuffer | Uint8Array): string {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  let s = "";
  for (const x of bytes) s += String.fromCharCode(x);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

interface RequestOptionsJSON {
  challenge: string;
  allowCredentials?: { id: string; type: string; transports?: string[] }[];
  [k: string]: unknown;
}

/** navigator.credentials.get's publicKey from the server's JSON. */
export function requestOptions(o: RequestOptionsJSON): PublicKeyCredentialRequestOptions {
  return {
    ...(o as object),
    challenge: b64uToBytes(o.challenge),
    allowCredentials: (o.allowCredentials || []).map((c) => ({
      ...c,
      type: "public-key" as const,
      id: b64uToBytes(c.id),
      transports: c.transports as AuthenticatorTransport[] | undefined,
    })),
  } as PublicKeyCredentialRequestOptions;
}

interface AssertionLike {
  id: string;
  rawId: ArrayBuffer;
  type: string;
  authenticatorAttachment?: string | null;
  response: {
    clientDataJSON: ArrayBuffer;
    authenticatorData: ArrayBuffer;
    signature: ArrayBuffer;
    userHandle?: ArrayBuffer | null;
  };
  getClientExtensionResults?: () => unknown;
}

/** The proof the decide carries: "passkey:" + the assertion as JSON. */
export function proofFromAssertion(c: AssertionLike): string {
  const r = c.response;
  const out: Record<string, unknown> = {
    id: c.id,
    rawId: bytesToB64u(c.rawId),
    type: c.type,
    clientExtensionResults: c.getClientExtensionResults ? c.getClientExtensionResults() : {},
    response: {
      clientDataJSON: bytesToB64u(r.clientDataJSON),
      authenticatorData: bytesToB64u(r.authenticatorData),
      signature: bytesToB64u(r.signature),
      ...(r.userHandle ? { userHandle: bytesToB64u(r.userHandle) } : {}),
    },
  };
  if (c.authenticatorAttachment) out.authenticatorAttachment = c.authenticatorAttachment;
  return PROOF_PREFIX + JSON.stringify(out);
}

/** Why a tap did not happen, in the card's words. */
export function passkeyErrorText(e: unknown): string {
  const name = (e as { name?: string } | null)?.name;
  if (name === "NotAllowedError" || name === "AbortError") {
    return "The passkey was not used (the prompt was closed or ran out). Try again, or use a code instead.";
  }
  if (name === "SecurityError" || name === "InvalidStateError") {
    return "Your passkey can't be used on this address. Use a code instead.";
  }
  const m = (e as { message?: string } | null)?.message;
  return m || "The passkey did not work. Try again, or use a code instead.";
}

// ── the two server calls ───────────────────────────────────────────────────

export function passkeyStatus(): Promise<ApiResult<PasskeyStatus>> {
  return getJSON<PasskeyStatus>("/api/approvals/passkey");
}

type Begin = () => Promise<ApiResult<{ options: RequestOptionsJSON }>>;

/** Ask the device for the proof of one approval. Throws an Error whose
 * message is the card's line when the Host refuses or the person does not
 * tap. */
export async function passkeyProof(begin: Begin): Promise<string> {
  if (!webAuthnAvailable()) throw new Error("This browser can't use a passkey. Use a code instead.");
  const r = await begin();
  if (!r.ok) throw new Error(r.message || "Your Host could not start the passkey check. Try again.");
  let cred: Credential | null;
  try {
    cred = await navigator.credentials.get({ publicKey: requestOptions(r.data.options) });
  } catch (e) {
    throw new Error(passkeyErrorText(e));
  }
  if (!cred) throw new Error(passkeyErrorText(null));
  return proofFromAssertion(cred as unknown as AssertionLike);
}

/** The proof for answering card `ticketId` with `optionId` (and `choices`,
 * the boxes ticked). */
export function ticketProof(ticketId: number, optionId: string, choices?: string[]): Promise<string> {
  return passkeyProof(() =>
    request("POST", "/api/approvals/passkey/ticket", {
      ticket_id: ticketId,
      option_id: optionId,
      ...(choices ? { choices } : {}),
    }),
  );
}

/** The proof for one request a card sends to a route of its own (the
 * machine-share card): exactly `body`, without the proof. */
export function requestProof(path: string, body: Record<string, unknown>): Promise<string> {
  return passkeyProof(() => request("POST", "/api/approvals/passkey/request", { path, body }));
}

/** What a card needs to offer the passkey: the mode and where one is set up. */
export interface PasskeyApprover {
  mode: PasskeyMode;
  setupUrl: string;
  prove: (ticketId: number, optionId: string, choices?: string[]) => Promise<string>;
}
