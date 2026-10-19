/**
 * Was a failed request the Host session ending? And when is it back?
 *
 * When the session ends, the router answers every request with a redirect to
 * the sign-in, which ends on the sign-in site — another origin — and a fetch()
 * that follows it rejects with TypeError "Failed to fetch" (2026-10-06: the run
 * cards showed exactly that, in red, from the moment the session ended).
 * A rejection alone cannot say why, so the answer comes from the router's
 * renewal, asked without following any redirect:
 *
 *  - framed by the desktop (the Workbench is same-origin with it, not one of
 *    the isolated apps — server/applications/kernel.py SDK_MANAGED_APPS), the
 *    desktop's own keeper asks (dashboard/base/js/session-keeper.js
 *    SessionKeeper.check): one probe for every window, and its "Your session
 *    expired" prompt is up the moment the answer is;
 *  - anywhere else (opened on its own, an older desktop), this page asks the
 *    renewal itself, and tells the desktop if there is one.
 *
 * Back again: the desktop announces it ({type: 'cseries:session', state: 'ok'})
 * when a renewal succeeds; a tab-wake asks again too.
 */

const RENEW = "/__host/renew";

export const SESSION_ENDED_VERSION = "Your session ended — sign in again to load this version.";
export const SESSION_ENDED_CHANGES = "Your session ended — sign in again to load these changes.";

type Keeper = { check?: () => Promise<string> };

function desktopKeeper(): Keeper | null {
  try {
    if (window.parent === window) return null;
    const k = (window.parent as unknown as { SessionKeeper?: Keeper }).SessionKeeper;
    return k && typeof k.check === "function" ? k : null;
  } catch {
    return null; // a parent on another origin
  }
}

/** A rejected fetch — what a redirect to the sign-in site looks like here. An
 * abort is not one. */
export function isFetchFailure(e: unknown): boolean {
  return !!e && typeof e === "object" && (e as { name?: string }).name === "TypeError";
}

/** Ask whether the Host session has ended. False on any doubt: an unknown
 * answer leaves the caller's own error standing. */
export async function sessionEnded(): Promise<boolean> {
  const k = desktopKeeper();
  if (k?.check) {
    try {
      return (await k.check()) === "expired";
    } catch {
      return false;
    }
  }
  try {
    const r = await fetch(RENEW, { cache: "no-store", credentials: "same-origin", redirect: "manual" });
    if (r.status !== 401) return false;
    try {
      if (window.parent !== window) window.parent.postMessage({ type: "cseries:session-check" }, "*");
    } catch {
      /* detached */
    }
    return true;
  } catch {
    return false;
  }
}

/** Call `back` once, when the session is back: the desktop announced a
 * successful renewal, or a tab-wake finds the session alive. Returns the
 * unsubscribe. */
export function onSessionBack(back: () => void): () => void {
  let done = false;
  const fire = () => {
    if (done) return;
    done = true;
    stop();
    back();
  };
  const onMessage = (ev: MessageEvent) => {
    // Only the desktop announces a return: this window's parent, on this
    // origin. Any other frame that can reach this window (an isolated app)
    // could post the same shape.
    if (window.parent === window || ev.source !== window.parent || ev.origin !== window.location.origin) return;
    const d = ev.data as { type?: string; state?: string } | null;
    if (d && d.type === "cseries:session" && d.state === "ok") fire();
  };
  const onWake = () => {
    if (document.visibilityState !== "visible") return;
    // Through the desktop this asks nothing while the session is known gone;
    // its own tab-wake ping is what announces the return.
    if (desktopKeeper()) return;
    fetch(RENEW, { cache: "no-store", credentials: "same-origin", redirect: "manual" })
      .then((r) => {
        if (r.status === 204) fire();
      })
      .catch(() => {});
  };
  function stop() {
    window.removeEventListener("message", onMessage);
    document.removeEventListener("visibilitychange", onWake);
  }
  window.addEventListener("message", onMessage);
  document.addEventListener("visibilitychange", onWake);
  return stop;
}
